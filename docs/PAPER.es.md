# gate-llm: servicios de IA bajo demanda en una sola GPU pequeña

**Un patrón medido para que una GPU modesta y propia sirva a varios servicios de IA heterogéneos, durmiendo y despertando bajo demanda**

Jeturing — octubre de 2026 · Licencia del texto y del código: MIT
Repositorio: https://github.com/Jeturing/gate-llm

*English version (main): [PAPER.md](PAPER.md)*

---

## Resumen

Las empresas que autoalojan IA enfrentan una aritmética incómoda:
cada servicio (un modelo de lenguaje, una voz sintetizada, un
transcriptor) se despliega como un residente permanente que ocupa memoria
de video (VRAM) las 24 horas, aunque su uso real sea intermitente y en
ráfagas. En una GPU pequeña — el hardware que una empresa realmente
tiene — dos residentes ya agotan la tarjeta, y el tercer servicio
simplemente no existe.

Este trabajo presenta **gate-llm**, un patrón mínimo y completo para
eliminar ese costo de residencia: un *portero* liviano (un único archivo
de Python con biblioteca estándar, sin dependencias) es dueño del puerto
público de cada servicio; el backend real permanece detenido y sin ocupar
VRAM; la primera petición lo despierta — y **espera**, retenida por el
portero, hasta que el endpoint de salud del backend declara el modelo
cargado —; y tras una ventana de inactividad sin peticiones en curso, el
portero lo detiene de forma segura.

El patrón se valida con mediciones de un despliegue real: una Tesla P4
de 8 GB sirviendo un LLM de 27 B de parámetros cuantizado a 1 bit y un
motor de síntesis de voz. Con ambos residentes, la VRAM libre era de
343 MiB; con todo dormido, la tarjeta entera — 8.192 MiB — queda libre.
El costo es la latencia en frío (24,5 s el despertar medido del servicio
de voz; 4,5–15,2 s el del LLM), pagada solo por la primera petición tras
cada sueño. El documento describe el diseño, la metodología de medición,
la evidencia completa, las limitaciones — incluida la lección de un
despliegue con tráfico casi continuo, donde la ventana de inactividad
resultó ser la variable que decide el ahorro — y la relación honesta con
el arte previo, del que este patrón es una reducción deliberada, no una
invención.

## 1. El problema

Autoalojar IA se ha vuelto barato en cómputo y caro en *residencia*. Los
motores de inferencia modernos cargan el modelo en VRAM al arrancar y lo
mantienen ahí: es lo correcto para latencia, y lo ruinoso para una GPU
pequeña compartida por varios servicios de uso esporádico:

- Un LLM de 27 B cuantizado a 1 bit ocupa ~4,5 GB de VRAM residente.
- Un motor de síntesis de voz con clonación ocupa ~3,4 GB residente.
- Una Tesla P4 tiene 8 GB.

Dos servicios ya suman ~7,9 GB: la tarjeta queda llena *estando ociosa*.
Añadir un tercer servicio (transcripción, un segundo modelo más ligero,
visión) es imposible sin retirar otro. La alternativa habitual — una GPU
por servicio, o alquilar GPU serverless en la nube — contradice el
motivo mismo de autoalojar: costo conocido, datos en casa, hardware ya
pagado.

La observación que ordena el diseño: **el uso real de estos servicios es
en ráfagas**. Un asistente interno responde en horario laboral; la voz
sintetiza por lotes; el modelo grande analiza cuando alguien pregunta.
La residencia permanente paga 24 h de VRAM por minutos de trabajo.

## 2. Diseño

### 2.1 Componentes

- **El portero** (`gate_llm_proxy.py`): un proceso por máquina, siempre
  activo, de consumo despreciable (Python estándar, hilos). Escucha en el
  puerto público de cada servicio registrado y reenvía el tráfico HTTP —
  incluido streaming, imprescindible para respuestas token a token — al
  backend real.
- **Los backends**: los servicios de IA tal cual existen (un servidor
  llama.cpp en contenedor, un motor TTS, mañana un STT), creados con el
  autoarranque desactivado y atados a puertos de *loopback*. Nada en
  ellos conoce al portero.
- **La configuración**: un único archivo JSON declara cada servicio:
  puertos, URL de salud, ventana de inactividad y los comandos de
  arranque/parada (`docker start/stop`, `systemctl start/stop`, o un
  proceso directo del que el portero es dueño). Es la única fuente de
  verdad; el menú de gestión (sección 5) edita ese mismo archivo.

### 2.2 Los cuatro mecanismos

1. **Propiedad del puerto.** El cliente (una aplicación, otro servicio,
   un navegador) llama al host y puerto de siempre. La sustitución del
   backend por el portero es invisible: misma API, mismo puerto.
2. **Retención de la petición en frío.** Si el backend está dormido, el
   portero ejecuta su arranque y *retiene* la petición del cliente —
   con un bloqueo por servicio, de modo que N peticiones simultáneas en
   frío comparten un único despertar — hasta que el backend declara
   salud o vence el plazo configurado (entonces, y solo entonces, la
   petición recibe un 502 explícito). Ninguna petición se pierde por
   llegar «demasiado pronto».
3. **Salud como contrato de preparación.** El portero sondea la URL de
   salud hasta obtener un estado esperado (200 en los motores medidos;
   un llama.cpp que aún carga su modelo responde 503, y ese detalle es
   exactamente el que separa «proceso arrancado» de «servicio listo»).
   Solo con salud verificada fluye el tráfico.
4. **Apagado seguro por inactividad.** Un vigilante por servicio detiene
   el backend cuando se cumplen *ambas* condiciones: transcurrió la
   ventana de inactividad desde la última petición **y** hay cero
   peticiones en curso. Una generación larga en marcha jamás es
   interrumpida por el temporizador.

Mecanismos auxiliares: endpoints de estado/despertar/apagado integrados
en el propio puerto (`/__gate-llm/status|wake|sleep`, que no cuentan
como actividad), y recarga en caliente de los parámetros de ejecución
(ventana, modelo activo) cuando la configuración cambia en disco — con
una regla de seguridad: el par arranque/parada y el modelo activo solo
se aplican con el servicio dormido, para que una parada nunca apunte al
proceso equivocado.

### 2.3 Elección de modelo por servicio

Un servicio puede declarar varios *modelos* (pares arranque/parada con
identidad propia — p. ej. un LLM grande de 27 B y uno ligero de 4 B
sobre el mismo puerto) y uno activo. Cambiar el modelo es: detener (si
está despierto), conmutar el activo en la configuración, y despertar.
El portero sirve siempre el activo; el resto duerme sin costo.

### 2.4 Ancla cálida en la caché de páginas (opcional)

El tiempo de despertar del LLM depende del estado de la caché de
páginas del sistema operativo. En la operación cotidiana — la
condición detrás del rango 4,5–15,2 s reportado en §4 — el archivo
del modelo a veces sigue residente en RAM y a veces debe venir del
disco. Un refinamiento opcional saca el disco de la ecuación:
*anclar* el archivo del modelo en la caché de páginas (leerlo a
caché al arrancar y fijarlo ahí, con páginas bloqueadas, al modo de
herramientas como `vmtouch`), de modo que las páginas del archivo
sobrevivan a la presión de caché y a los reinicios del portero, y
todo despertar lea el modelo desde RAM. Los backends siguen
deteniéndose y liberando toda la VRAM; lo que permanece residente
son solo las páginas del archivo en la RAM ordinaria del sistema.

Medido en el despliegue de referencia, bajo una condición
controlada y distinta del rango cotidiano anterior: con las cachés
soltadas deliberadamente (un arranque frío verdadero) y sin ancla,
el motor estuvo listo en **23,4 s** y el primer token llegó a los
**23,8 s** (ruta del consumidor, chat en streaming). Con el ancla
activa — el archivo del modelo, de 3,8 GB, verificado residente y
bloqueado al 100 % —, tres despertares por la misma ruta dieron
motor listo en **12,0–16,2 s** y primer token en **13,6–18,1 s**:
el peor caso frío verdadero se reduce aproximadamente a la mitad.
El costo es **3,54 GiB de RAM retenidas de forma permanente** en
una máquina de 16 GiB: el piso dormido sube de ~2,0 GiB a ~5,7 GiB,
mientras el pico despierto no crece — las páginas ancladas se
comparten con el mapeo que el propio servidor hace del archivo —,
dejando ~10 GiB de margen. La varianza que queda ya no viene del
archivo del modelo: viene del arranque del contenedor, las páginas
de binarios y librerías, y la inicialización de CUDA.

### 2.5 Qué no intenta ser

No es un orquestador: no crea backends, no los replica, no balancea
entre máquinas. No es un servidor de modelos: no toca la inferencia.
Es, deliberadamente, la capa fina que falta entre «el servicio existe»
y «el servicio ocupa la GPU solo cuando trabaja».

### 2.6 Portabilidad: el entorno se detecta, no se supone

El portero y cada herramienta del kit son biblioteca estándar de
Python, y una capa de entorno (`gate_llm_env.py`) detecta al
arrancar lo que la máquina realmente ofrece — sistema operativo,
un systemd *operativo* (no solo instalado), Docker, el binario
`nvidia-smi` (en Windows vive en los directorios propios de NVIDIA,
no en un PATH estilo POSIX), entornos de cuadernos alojados como
Colab — en vez de suponer un servidor Linux. La supervisión de cada
backend es una estrategia, elegida por servicio:

| Entorno | Supervisión del backend | Residencia del portero |
|---|---|---|
| Linux con systemd | unidades `systemd` o contenedores `docker` | unidad systemd |
| Linux sin systemd — contenedores, WSL, Colab | `process`: spawn desacoplado con registro de PID | proceso desacoplado, o el supervisor del anfitrión |
| Windows | `process` (o `docker` donde Docker Desktop corre el backend) | Tarea Programada — dicho con claridad: una tarea, no un servicio nativo de Windows |

La estrategia `process` es el piso portable: lanzar el backend
desacoplado (`start_new_session` en POSIX;
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` en Windows), registrar
su PID en un pequeño archivo JSON, y detener primero con suavidad —
SIGTERM al grupo de procesos en POSIX; en Windows `taskkill /T`,
escalando a `/F`, donde el paso suave es «best-effort»: un límite
honesto de esa plataforma sin un envoltorio de servicio. Un portero
reiniciado re-adopta un backend desde su registro de PID solo
después de que el contrato de salud responda por él; nunca mata un
proceso que no haya verificado como su backend. La regla de parada
segura — nunca detener con peticiones en curso — está por encima de
toda estrategia: la supervisión decide *cómo* arranca y se detiene
un backend, nunca *si* es seguro detenerlo.

El instalador (`install.py`) es el mismo código en ambos sistemas
operativos: imprime el entorno detectado, elige la residencia que
la máquina soporta y termina verificando el ciclo. Y el set de
pruebas del kit — backends ficticios, sin GPU — corre en
integración continua sobre runners de Ubuntu y Windows, de modo
que la afirmación de portabilidad se re-verifica en público con
cada push, no solo se afirma aquí.

## 3. Metodología de medición

Despliegue de referencia: máquina virtual con ~8 GB de RAM y una única
GPU **Tesla P4 (8.192 MiB)** pasada a la VM; sistema Linux con systemd
y Docker. Servicios:

- **LLM**: modelo de 27 B de parámetros, cuantización de 1 bit
  (formato GGUF Q1_0, ~3,8 GB en disco), servido por llama.cpp tras el
  portero. Huella residente medida: ~4,5 GB de VRAM.
- **TTS**: motor open-source de clonación de voz zero-shot, servido en
  contenedor tras el portero. Huella residente medida: ~3,4 GB de VRAM.

Procedimientos:

- **VRAM**: `nvidia-smi` (memoria usada/libre por proceso y total) en
  cada estado: ambos residentes, TTS dormido, todo dormido.
- **Despertar en frío**: petición real a través del portero con el
  backend detenido; se mide hasta (a) salud con el modelo cargado y
  (b) primera respuesta completa extremo a extremo, según el tipo de
  petición.
- **Rendimiento en caliente**: banco de pruebas propio
  (`tools/bench.py`, incluido) contra el endpoint compatible con
  OpenAI: completions en streaming, prompt fijo de trabajo (~460
  tokens), generaciones sostenidas de ~120 tokens, varias corridas por
  condición; se reportan tiempo al primer token (TTFT) y tokens/segundo
  de generación. La condición «contención» genera un segundo flujo en
  paralelo, que reproduce el uso real del gateway compartido.
- **Ciclo completo**: `tools/verify_cycle.sh` (incluido) ejecuta
  dormido → petición → despierto → responde → inactividad → dormido,
  con VRAM por etapa, sin intervención.
- **Tráfico real**: auditoría de los registros del propio despliegue
  durante 3,3 días para caracterizar la demanda (sección 6.4).

Toda cifra del documento sale de estas mediciones; donde un valor
depende de la caché o del estado previo, se reporta el rango observado
y se dice de qué depende.

## 4. Resultados

### 4.1 Memoria: la tarjeta vuelve a ser un fondo común

| Estado | VRAM libre (de 8.192 MiB) |
|---|---|
| Ambos servicios residentes (antes del patrón) | 343 MiB |
| TTS dormido, LLM residente | 3.628 MiB |
| **Todo dormido** | **8.192 MiB (0 MiB en uso)** |

La RAM del host acompañó el mismo movimiento: de ~10 GiB con todo
residente a ~2,0 GiB con todo dormido. Dicho de otro modo: fuera de los
minutos de trabajo real, la máquina recupera íntegros sus dos recursos
escasos.

### 4.2 Costo del despertar

| Servicio | Despertar en frío |
|---|---|
| TTS | **24,5 s** hasta salud con el modelo cargado; la primera síntesis tras despertar fue válida |
| LLM | **4,5–15,2 s** hasta motor listo, según el estado de la caché; primera respuesta completa extremo a extremo: **~14 s** (petición corta de decisión) y **~24 s** (chat en streaming) |
| LLM, con el archivo del modelo anclado en la caché de páginas (§2.4) | **12,0–16,2 s** hasta motor listo, primer token **13,6–18,1 s** (n=3 despertares, frío verdadero: cachés soltadas). En la misma condición de frío verdadero sin el ancla: 23,4 s / 23,8 s. Costo: 3,54 GiB de RAM retenidas |

El despertar es el precio único y visible del patrón, y se paga una
sola vez por sueño: la primera petición. Todas las siguientes, hasta
que la ventana de inactividad vuelva a cumplirse, son en caliente.

### 4.3 Rendimiento en caliente (sin cambios respecto a residente)

| Condición | TTFT | Generación |
|---|---|---|
| Sin contención | 0,48–0,65 s | 14,5 tok/s |
| Con un segundo flujo en paralelo | estirado | 8,2–8,3 tok/s |
| Caché de prompt fría, motor ya en marcha | 2,6–5,2 s | 14,5 tok/s |

El patrón no añade latencia medible en caliente (el portero es un
reenvío local en streaming); la contención observada es la del propio
motor al compartir la GPU entre dos flujos, no un efecto del portero.

### 4.4 Ventanas de referencia

Las ventanas que el despliegue de referencia usa en producción — y que
el ejemplo del repositorio trae — son **30 min para el LLM** y
**20 min para el TTS**: lo bastante largas para absorber pausas
naturales de una sesión de trabajo sin pagar despertares, lo bastante
cortas para que la tarjeta pase la noche libre.

## 5. Adopción: un comando y un menú

Un patrón de infraestructura solo existe si instalarlo cuesta menos
que el problema que resuelve. El paquete incluye:

- **Autoinstalador de un comando** (`install.py` — el mismo archivo
  Python en Linux y Windows; `install.sh` queda como atajo fino
  POSIX): detecta el entorno (§2.6), comprueba requisitos y falla en
  voz alta, con la solución, cuando falta uno (Python; GPU visible
  para `nvidia-smi`; Docker solo si la config lo usa); registra la
  residencia del portero con lo que la máquina soporte — unidad
  systemd, Tarea Programada de Windows, o proceso desacoplado con su
  lanzador generado —; crea la configuración desde la plantilla *sin
  sobrescribir jamás* una existente; y cierra verificando el patrón
  contra el primer servicio (dormido → petición → despierta →
  responde, con tiempos). Es idempotente: correrlo dos veces es
  seguro.
- **Menú de terminal** (`tools/menu.py`, biblioteca estándar):
  lista servicios y modelos con estado y VRAM en vivo; cambia el
  modelo activo aplicando el ciclo detener–conmutar–despertar;
  registra y retira servicios y modelos; despierta y duerme a mano;
  ajusta ventanas. Valida antes de guardar (esquema, puertos en uso —
  incluida una sonda real de enlace —, salud alcanzable cuando el
  servicio está despierto) y escribe de forma atómica con respaldo.
- **Kit de replicación**: el banco de pruebas de TTFT/tok·s y el script
  de verificación del ciclo completo, para que quien replique el
  patrón obtenga — y pueda publicar — sus propios números con el mismo
  procedimiento.

## 6. Limitaciones y lecciones

### 6.1 La latencia en frío excluye a ciertos clientes

Un cliente interactivo con un timeout de 5 s no sobrevivirá a un
despertar de 24,5 s. El patrón sirve a clientes tolerantes (lotes,
herramientas internas, asistentes con espera aceptable, APIs con
timeout configurado) y perjudica a los impacientes. La mitigación
parcial — despertar anticipado por horario o por evento — está en la
hoja de ruta, no en esta versión.

### 6.2 La VRAM se comparte en el tiempo, no se multiplica

Dos servicios despiertos a la vez deben caber juntos. Si la demanda
real hace coincidir a todos, el patrón degrada a «todo residente» y el
ahorro desaparece: la ganancia existe porque el uso real es en
ráfagas y desfasado. Cuando dos despertares compiten por la misma
VRAM, esta versión los serializa por orden de llegada; la cola con
prioridades es mejora futura.

### 6.3 Solo backends sin estado (o con estado a salvo)

Detener por inactividad exige que la parada sea inocua: sesiones o
trabajos a medias dentro del backend se pierden. Los motores de
inferencia y de síntesis habituales cumplen (su estado es el modelo,
que se recarga); un backend con estado en memoria sin persistir, no.

### 6.4 La lección del tráfico continuo: la ventana manda

La auditoría de 3,3 días del despliegue de referencia arrojó el
resultado más instructivo del proyecto: la puerta del LLM atiende
también otro tráfico casi continuo — del orden de un centenar de
peticiones por hora a todas horas — y el hueco más largo sin llamadas
en esos 3,3 días fue de **51 minutos**. Con una ventana de inactividad
mayor que los huecos naturales, el modelo apenas habría dormido y el
patrón habría sido decorativo. Con la ventana en **30 minutos**, el
modelo durmió **varias veces** en ese mismo período, y la tarjeta se
liberó horas enteras por la noche.

La lección generalizable: **en un servicio compartido y ocupado, el
ahorro no lo decide el patrón, lo decide la ventana**. Medir primero
los huecos naturales del tráfico (la auditoría de registros basta) y
fijar la ventana por debajo del hueco típico es parte de la receta, no
un detalle de ajuste.

### 6.5 Un solo portero

El portero es un proceso único por máquina: es también un punto único
de paso. Su supervisor lo reinicia si muere (unidad systemd, tarea
programada, gestor de servicios — §2.6), y los backends dormidos no
pierden nada; pero un backend despierto solo sirve a través de él. En
la escala de una GPU y una empresa, es el mismo nivel de
riesgo que el del propio motor de inferencia; en otra escala, no.

## 7. Trabajo relacionado

Este patrón no nace en el vacío; es una reducción consciente de ideas
consolidadas al caso mínimo de una GPU propia:

- **Activación por socket (systemd)**: el sistema operativo arranca el
  servicio a la primera conexión y le pasa el socket ya aceptado. Es
  el ancestro directo; opera sobre procesos y sockets, sin noción de
  salud, de modelo cargándose ni de VRAM, y sin la retención ordenada
  de peticiones concurrentes en frío que aquí resuelve el portero.
- **Knative Serving**: su *Activator* recibe el tráfico cuando no hay
  réplicas, retiene las peticiones, dispara el escalado desde cero y
  las reenvía al estar listo el backend — la misma forma que este
  portero, sostenida por todo un plano de control de Kubernetes.
  **KEDA** aporta el escalado a cero gobernado por eventos en el mismo
  ecosistema. gate-llm toma la forma y elimina el clúster.
- **Ollama (`keep_alive`)**: un motor concreto que carga el modelo a la
  primera petición y lo descarga tras 5 minutos de inactividad por
  defecto. Demuestra el apetito por el comportamiento; gate-llm lo
  desacopla del motor y lo extiende a cualquier backend HTTP, con
  parada del proceso completo (no solo descarga del modelo) y con
  varios servicios coordinados sobre la misma GPU.
- **llama-swap** (https://github.com/mostlygeek/llama-swap): el
  vecino más cercano. Un proxy que arranca servidores locales de
  inferencia bajo demanda cuando una petición nombra su modelo,
  espera a que su endpoint de salud responda antes de servir y los
  descarga tras un TTL de inactividad. Su unidad de gestión es el
  *modelo de un motor de inferencia*, enrutado por nombre de modelo
  tras un único endpoint. gate-llm gobierna *servicios completos
  del sistema* — unidades systemd y contenedores, heterogéneos y
  no solo de inferencia — tras un portero por servicio, empaquetado
  como receta medida para una GPU propia de 8 GB que sirve los
  servicios heterogéneos de IA de una empresa.
- **Sleepy LLM** (https://github.com/ac101m/sleepy-llm): un proxy
  mínimo que despierta un backend LLM dormido — incluso la máquina
  misma, por Wake-on-LAN — reteniendo la petición hasta que el
  backend responde. Gestiona un único backend, sin empaque, kit de
  medición ni gobierno multiservicio: evidencia independiente y
  casi simultánea de que el gesto central está en el aire.
- **Sablier** (https://sablierapp.dev/): escalado a cero genérico
  para contenedores, con una estrategia *blocking* que retiene la
  petición hasta que la carga está arriba. Es el gemelo de
  infraestructura del portero, sin capa de inferencia: su «listo»
  es un contenedor arrancado, no un modelo cargado bajo contrato de
  salud.
- **Carga y descarga internas del motor**: el modo router de
  llama.cpp carga y descarga modelos dentro de un servidor que
  permanece vivo, y el sleep mode de vLLM descarga los pesos sin
  detener el proceso. Ambos actúan *dentro* de un motor; el portero
  detiene y arranca *servicios completos*, corran el motor que
  corran — o ninguno de inferencia.
- **GPU serverless comercial** (RunPod, Modal, Baseten, Replicate y
  otros): venden despertar bajo demanda como servicio, con su
  arranque en frío medido en segundos y su facturación por uso. Son
  la prueba de mercado del patrón y, a la vez, lo contrario de este
  proyecto en lo esencial: allí la GPU es de otro; aquí es la tuya.

La contribución que se reclama es estrecha y comprobable — no el
proxy que despierta bajo demanda en sí, que existe, como muestran
los vecinos de arriba, sino la receta completa que lo rodea: un
portero por servicio que gobierna servicios completos heterogéneos
(un LLM, una voz) turnándose una sola GPU propia de 8 GB, un
instalador y un menú de gestión, reglas operativas medidas (el
tráfico de salud no despierta ni cuenta como actividad; nunca
parar con peticiones en curso) y números publicados de despertar,
latencia y contención — funcionando y medida sobre el hardware que
una empresa realmente tiene.

## 8. Mejoras abiertas

El documento [MEJORAS.md](MEJORAS.md) desarrolla las rutas conocidas,
cada una con su ganancia, su costo y su riesgo: cola de despertares
con prioridad cuando dos servicios compiten por la VRAM;
precalentamiento por horario; medición de «minutos despierto» por
servicio y por cliente como base de cobro por uso; un modelo ligero
de diálogo residente junto al grande bajo demanda; multi-GPU;
activación por socket de systemd donde encaje mejor que el portero;
y una interfaz web para el menú. Ninguna está implementada en esta
versión y el texto no las presenta como existentes.

## 9. Conclusiones

Una GPU pequeña no es pequeña: es pequeña *su residencia*. Con un
portero que retiene la petición en frío en vez de fallarla, un
contrato de salud que distingue «arrancado» de «listo», y una parada
que nunca corta trabajo en curso, los servicios de IA de una
empresa pueden compartir una sola tarjeta de 8 GB durmiendo
entre usos — medido: de 343 MiB libres con todo residente a
8.192 MiB libres con todo dormido — pagando un único costo conocido,
el despertar, solo en la primera petición de cada ráfaga. La variable
que el operador debe gobernar no es técnica sino de observación: la
ventana de inactividad frente a los huecos reales de su tráfico.

Quien replique la receta con el kit incluido debería encontrar en su
hardware lo que aquí se afirma del nuestro. Esa es la afirmación
central del trabajo, y está escrita para poder ser refutada con
números.

## Referencias

1. systemd.socket — activación de servicios por socket.
   https://www.freedesktop.org/software/systemd/man/systemd.socket.html
2. Knative Serving — escalado automático y escalado a cero; el
   Activator. https://knative.dev/docs/
3. KEDA — escalado gobernado por eventos para Kubernetes.
   https://keda.sh
4. Ollama — documentación y API (`keep_alive`, descarga por
   inactividad). https://github.com/ollama/ollama
5. llama.cpp — servidor de inferencia usado en las mediciones.
   https://github.com/ggerganov/llama.cpp
6. NVIDIA System Management Interface (`nvidia-smi`) — medición de
   VRAM. https://developer.nvidia.com/
7. llama-swap — proxy para servidores locales de inferencia con
   arranque bajo demanda y descarga por inactividad.
   https://github.com/mostlygeek/llama-swap
8. Sleepy LLM — proxy que despierta bajo demanda un backend LLM
   dormido. https://github.com/ac101m/sleepy-llm
9. Sablier — escalado a cero para contenedores con estrategia
   blocking. https://sablierapp.dev/

---

*gate-llm es software libre (MIT). Texto y mediciones: Jeturing,
octubre de 2026. Las cifras corresponden al despliegue de referencia
descrito en la sección 3; tu hardware dará otras — el kit de
replicación existe para medirlas.*
