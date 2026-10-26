# gate-llm — opciones de mejora

Rutas conocidas y honestas para llevar el patrón más lejos. Ninguna está
implementada en esta versión: cada una dice qué gana, qué cuesta y qué
riesgo trae, para que quien adopte gate-llm decida con los ojos abiertos
— y para que nadie confunda la hoja de ruta con el producto.

*English summary at the end.*

---

## 0. HECHA en esta versión: portabilidad Windows / Linux sin systemd

Lo que antes era supuesto — Linux con systemd y Docker — ahora es
detección: una capa de entorno reporta el sistema operativo, si systemd
está operativo, si hay Docker y dónde vive `nvidia-smi`, y cada servicio
declara su estrategia de supervisión (`systemd`, `docker`, `command` o
`process` — proceso plano desacoplado con registro de PID, la vía de
Colab, de Linux sin systemd y de Windows). El instalador es un solo
`install.py` para ambos sistemas (residencia: unidad systemd, Tarea
Programada, o proceso desacoplado con lanzador), la verificación del
ciclo es `tools/verify_cycle.py`, y el set de pruebas corre en CI sobre
Ubuntu y Windows en cada push. Límites honestos, en el §2.6 del paper:
en Windows la residencia es una tarea programada, no un servicio
nativo, y la parada suave de un proceso plano es «best-effort».

Replicación externa verificada el 2026-10-26 (§6.6 del paper): el ciclo
completo pasó en Google Colab — Tesla T4, sin systemd ni Docker,
estrategia `process` — con despertar de 4,6 s para un modelo de 3B. La
misma sesión dejó la advertencia que acompaña a esta ruta: un backend
puede correr en CPU en silencio aunque su binario diga «CUDA»; la
señal es que la VRAM no se mueve entre dormido y despierto, y conviene
comprobarla antes de creer cualquier cifra de rendimiento.

## 1. Cola de despertares con prioridad

**Qué es.** Cuando dos servicios despiertan a la vez y juntos no caben
en la VRAM, hoy manda el orden de llegada. La mejora: una cola única de
despertares con prioridad por servicio (y, si hace falta, desalojo del
servicio ocioso de menor prioridad para hacer sitio).

**Qué gana.** Determinismo en el recurso escaso: la voz que atiende a un
cliente no espera detrás de un lote de análisis.

**Qué cuesta.** Un árbitro con estado compartido entre servicios: el
portero pasa de «un dueño por puerto» a coordinador de la tarjeta, y
cada parada/desisión debe razonarse contra la cola.

**Riesgo.** Desalojo equivocado = despertar en cadena (un servicio
expulsa a otro, que vuelve a despertar y expulsa al primero). Exige
histéresis: tiempos mínimos de residencia antes de poder ser desalojado.

## 2. Precalentamiento por horario (y por evento)

**Qué es.** Despertar un servicio minutos antes de su uso previsible —
a las 8:55 si el equipo entra a las 9:00 — usando el endpoint
`/__gate-llm/wake` desde cron/systemd timers, o ante eventos externos
(una reserva, un turno que empieza).

**Qué gana.** La primera petición del día deja de pagar el despertar:
la latencia en frío desaparece justo donde más se nota.

**Qué cuesta.** Casi nada en código (el endpoint ya existe); el costo
es de *acierto*: cada precalentamiento que no se usa paga la ventana de
inactividad completa de VRAM ocupada.

**Riesgo.** Bajo. El peor caso es volver, por horario, al mundo
residente que el patrón quería evitar. Conviene medir aciertos/fallos
antes de multiplicar horarios.

## 3. Medición de «minutos despierto» por servicio y por cliente

**Qué es.** El portero ya sabe cuándo despierta y cuándo duerme cada
backend. Registrar esos intervalos (y, donde el tráfico se pueda
atribuir, por cliente o por clave de API) convierte el patrón en una
base de medición: minutos despierto por servicio, por cliente, por
franja.

**Qué gana.** La base técnica del **cobro por uso**: un servicio de IA
que duerme 23 h y trabaja 1 h puede facturarse por su uso real de GPU,
no por una residencia ficticia. Es también la métrica que justifica la
siguiente GPU (o demuestra que no hace falta).

**Qué cuesta.** Contabilidad cuidadosa: relojes, reinicios del portero
con un backend despierto, intervalos que cruzan la medianoche.
Nada exótico; todo aburrido y necesario.

**Riesgo.** Medir mal es peor que no medir si la cifra alimenta una
factura. Publicar la metodología junto a la métrica, como hace este
proyecto con sus números, es parte de la mejora.

## 4. Modelo ligero residente junto al grande bajo demanda

**Qué es.** Dejar un modelo pequeño (3–4 B) siempre residente para el
diálogo rápido y rutinario, y despertar el grande (27 B) solo cuando
la tarea lo exige: análisis largo, documentos, decisiones.

**Qué gana.** Lo mejor de dos mundos: primer token casi inmediato en lo
cotidiano y capacidad completa cuando importa, en la misma tarjeta.

**Qué cuesta.** El ligero ocupa su VRAM *siempre* (es la excepción
consciente al patrón) y alguien debe decidir qué petición va a cuál:
un clasificador, una regla por endpoint, o el propio modelo ligero
escalando al grande.

**Riesgo.** Que «lo cotidiano» crezca hasta comerse la tarjeta y el
grande ya no quepa al despertar. La suma de huellas debe seguir
respetando el límite físico, con el ligero contado como residente.

## 5. Multi-GPU

**Qué es.** Varios porteros (o uno con noción de tarjeta) repartiendo
servicios entre 2+ GPUs: afinidad servicio→tarjeta, y despertares que
eligen la tarjeta con sitio.

**Qué gana.** Escala horizontal sin cambiar el patrón: cada tarjeta
sigue siendo un fondo común que duerme por partes.

**Qué cuesta.** La coordinación de la mejora 1, multiplicada: ahora el
árbitro decide también *dónde* despierta cada servicio, y mover un
backend de tarjeta puede implicar recargar el modelo desde disco.

**Riesgo.** Complejidad de explotación: más estados, más combinaciones
de fallo. Tiene sentido cuando una segunda tarjeta ya es un hecho, no
como anticipación.

## 6. Activación por socket de systemd donde encaje mejor

**Qué es.** Para backends que son *un proceso con un socket* y cuyo
«modelo» carga el propio proceso al primer uso, systemd puede activar
por socket sin portero: menos piezas móviles.

**Qué gana.** Usar la herramienta del sistema, probada durante años,
donde sus garantías bastan.

**Qué cuesta.** Perder lo que el portero añade: el contrato de salud
(un socket aceptado no es un modelo cargado), la retención ordenada
de ráfagas en frío, el estado unificado de varios servicios y la
contabilidad de minutos despierto.

**Riesgo.** Aplicarlo donde no basta y descubrir en producción la
diferencia entre «proceso vivo» y «servicio listo». La regla práctica:
socket activation para servicios sin carga de modelo; gate-llm para
todo lo que cargue algo pesado al arrancar.

## 7. Interfaz web para la gestión

**Qué es.** El menú de terminal, como panel web: mismo archivo de
configuración, mismas validaciones, estado en vivo sin refrescar a
mano.

**Qué gana.** Accesibilidad para quien no vive en la terminal y una
vista de conjunto (tarjeta, servicios, minutos despierto) de un
vistazo.

**Qué cuesta.** Superficie de ataque nueva: un panel que arranca y
detiene servicios de IA es infraestructura sensible; exigiría
autenticación seria, sesiones y la disciplina de cualquier panel de
la casa (login propio, nada de datos operativos sin autenticar).

**Riesgo.** Que la comodidad abra una puerta que la terminal no tenía.
Por eso es mejora y no parte del núcleo: el patrón funciona entero
sin web.

---

## English summary

Known, honest routes beyond this version — none of them implemented
here; each with its gain, cost and risk in the Spanish text above:

0. **DONE in this version: Windows / Linux portability** — environment
   detection instead of assumptions; per-service supervision strategies
   (`systemd`, `docker`, `command`, `process`); one cross-platform
   `install.py`; the test suite running in CI on Ubuntu and Windows.
   Honest limits (a Scheduled Task is not a native Windows service;
   graceful stop of a plain process on Windows is best-effort) are in
   §2.6 of the paper. External replication verified 2026-10-26 (paper
   §6.6): the full cycle passed on Google Colab — Tesla T4, no systemd
   or Docker, `process` strategy — waking a 3B model in 4.6 s; the same
   session's warning travels with this route: a backend can silently
   run on CPU despite a "CUDA" build, and the tell is VRAM that never
   moves between asleep and awake.
1. **Wake queue with priority** when two services compete for the
   same VRAM (gain: determinism; risk: eviction thrashing without
   minimum-residency hysteresis).
2. **Scheduled / event-driven pre-warming** via the existing wake
   endpoint (gain: no cold first request of the day; cost: wasted
   windows when the prediction misses).
3. **Awake-minutes metering** per service and per client — the
   technical basis of usage billing, and the metric that justifies
   (or rules out) the next GPU.
4. **A light dialog model resident** next to the large on-demand one
   (gain: instant first token for routine work; risk: the resident
   footprint eating the card).
5. **Multi-GPU** placement (gain: horizontal scale; cost: a smarter
   arbiter deciding *where* each service wakes).
6. **systemd socket activation** where a backend has no model to
   load (gain: fewer moving parts; cost: no health contract, no
   unified state or metering).
7. **A web management UI** over the same config (gain: accessibility;
   risk: a new attack surface on sensitive infrastructure — it needs
   real authentication, which is why it is not in the core).
