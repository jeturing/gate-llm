# gate-llm

**Servicios de IA bajo demanda en una sola GPU pequeña.**

*Read this in English: [README.md](README.md)*

Una GPU pequeña tiene sitio para un servicio de IA residente — quizá dos,
si son pequeños. Pero una empresa no corre un solo servicio de IA: corre un
modelo de lenguaje, una voz, mañana un transcriptor, y cada uno ocupa la
VRAM todo el día, despierto y ocioso, porque nadie sabe cuándo llegará la
siguiente petición.

gate-llm elimina ese impuesto de residencia. Un portero liviano es dueño
del puerto público de cada servicio. El backend real duerme **detenido**,
sin ocupar VRAM. La primera petición lo despierta: el portero arranca el
backend, espera a que su endpoint de salud declare el modelo cargado y
recién entonces reenvía la petición — **la petición en frío espera unos
segundos en vez de fallar**. Tras una ventana de inactividad sin nada en
curso, el portero detiene el backend otra vez y la memoria vuelve al fondo
común, libre para lo que necesite la GPU después.

Un solo archivo de Python 3 con biblioteca estándar. Sin dependencias,
sin Kubernetes, sin cuenta en la nube.

## Qué es — y qué no es

La idea de arrancar servicios bajo demanda es antigua y honorable, y este
proyecto no pretende haberla inventado:

* **La activación por socket de systemd** arranca servicios a la primera
  conexión desde hace años — a nivel de un proceso y un socket, no de un
  servicio de GPU con un modelo que cargar y un contrato de salud.
* **Knative / KEDA** industrializaron el «escalar a cero» en Kubernetes,
  con un activador que retiene las peticiones durante el arranque en
  frío. gate-llm es lo que ese patrón llega a ser cuando no hay clúster:
  una GPU pequeña, un puñado de servicios, un archivo JSON.
* **Ollama** carga un modelo al pedirlo y lo descarga tras un período de
  inactividad (`keep_alive`, 5 minutos por defecto) — dentro de un solo
  motor, para sus propios modelos. gate-llm generaliza el comportamiento
  a *cualquier* backend HTTP: contenedores, unidades systemd, procesos
  directos.
* **Las plataformas de GPU serverless** venden la misma economía como
  servicio en la nube. gate-llm es para la GPU que ya tienes.

Lo específico aquí es la **receta completa y medida** para que una GPU
modesta y propia sirva a varios servicios de IA heterogéneos de una
organización pequeña — un LLM hoy, una voz mañana — con retención de la
petición en vez de fallo, despertar gobernado por salud, apagado seguro
por inactividad y números reales de una máquina real, reproducibles con
los scripts de este repositorio.

## Cómo funciona

```
                        ┌───────────────────────────────┐
   cliente ── petición ─▶  gate-llm (siempre activo,   │
                        │  mínimo)                      │
                        │                               │
                        │  ¿dormido? ─ arranca backend ─▶  backend (LLM / TTS / STT)
                        │            espera su salud       solo en loopback,
                        │            reenvía la petición   detenido si ocioso
                        │  ventana cumplida, 0 en curso ─ detiene el backend
                        └───────────────────────────────┘
```

Cuatro mecanismos, y solo cuatro:

1. **El portero es dueño del puerto.** Los backends se atan a loopback y
   se crean con el autoarranque desactivado. Los clientes no cambian
   nada: mismo host, mismo puerto, misma API.
2. **La petición en frío se retiene, no falla.** A la primera petición,
   el portero ejecuta el comando de arranque del servicio y *mantiene al
   cliente esperando* hasta que el backend está sano. Los clientes
   necesitan un timeout mayor que el tiempo de despertar — ese es el
   intercambio central del patrón, y es deliberado.
3. **La salud es el contrato.** «Despierto» significa que la URL de
   salud responde con un estado esperado. Un proceso que arrancó pero
   sigue cargando su modelo no recibe tráfico.
4. **La parada es segura.** Un vigilante detiene el backend solo tras la
   ventana de inactividad *y* con cero peticiones en curso. Una
   generación larga nunca se corta.

Endpoints integrados en el puerto de cada servicio (nunca cuentan como
actividad):

| Endpoint | Efecto |
|---|---|
| `GET /__gate-llm/status` | Estado en JSON; nunca despierta el backend |
| `POST /__gate-llm/wake` | Despierta ahora y espera a que esté sano |
| `POST /__gate-llm/sleep` | Detiene ahora (409 si hay una petición en curso) |

## Medido en hardware real

Despliegue de referencia: una máquina virtual con ~8 GB de RAM y una sola
**Tesla P4 (8.192 MiB)**, sirviendo un LLM de 27 B de parámetros
cuantizado a 1 bit (llama.cpp, GGUF, ~3,8 GB en disco, ~4,5 GB de VRAM) y
un motor TTS open-source de clonación de voz zero-shot (~3,4 GB de VRAM).
Metodología completa y discusión en [el paper](docs/PAPER.es.md) (versión principal en inglés: [docs/PAPER.md](docs/PAPER.md)).

| Medición | Valor |
|---|---|
| VRAM libre con ambos servicios residentes | 343 MiB |
| VRAM libre con el TTS dormido (LLM residente) | 3.628 MiB |
| VRAM libre con **todo dormido** | **8.192 MiB (0 en uso)** |
| Despertar en frío del TTS (petición → salud con el modelo cargado) | 24,5 s |
| Despertar en frío del LLM (motor listo; según la caché) | 4,5–15,2 s |
| Primera respuesta completa del LLM en frío, extremo a extremo | ~14 s (petición corta) · ~24 s (chat en streaming) |
| LLM en caliente — tiempo al primer token | 0,48–0,65 s |
| LLM en caliente — velocidad de generación | 14,5 tok/s |
| LLM con un segundo flujo en paralelo | 8,2–8,3 tok/s |
| Primer token con la caché de prompt fría (motor ya en marcha) | 2,6–5,2 s |
| Ventanas de inactividad de referencia | LLM 30 min · TTS 20 min |
| RAM del host, todo despierto → todo dormido | ~10 GiB → ~2,0 GiB |
| Despertar del LLM con el archivo del modelo anclado en la caché de páginas del sistema | [[PENDIENTE: ancla de caché]] |

Una advertencia honesta del mismo despliegue: la puerta del LLM atiende
también otro tráfico casi continuo (del orden de un centenar de
peticiones por hora, a todas horas; el hueco más largo sin llamadas en
3,3 días fue de 51 minutos). Aun así, con la ventana en 30 minutos el
modelo durmió varias veces en ese período. **La ventana de inactividad
es la perilla que decide cuánto se ahorra** — lee las limitaciones del
paper antes de elegir la tuya.

## Instalación (un comando, la vía recomendada)

```bash
git clone https://github.com/Jeturing/gate-llm.git
cd gate-llm
sudo ./install.sh
```

El instalador comprueba los requisitos y **falla en voz alta, con la
solución, cuando falta uno** (Python 3.8+, systemd, una GPU NVIDIA vía
`nvidia-smi`, Docker si tu config maneja contenedores). Instala el
portero como servicio systemd, crea `/etc/gate-llm/services.json` desde
el ejemplo *solo si no existe ninguno* (tu config nunca se sobrescribe),
arranca el servicio y verifica el patrón de extremo a extremo: dormido →
petición → despierta → responde, con tiempos. Banderas útiles:
`--config ARCHIVO` (instala tu propia config), `--skip-gpu-check`
(servicios solo de CPU), `--no-verify`.

Después edita la config para declarar *tus* servicios y reinicia:

```bash
sudo nano /etc/gate-llm/services.json
sudo systemctl restart gate-llm
```

Tus backends deben existir de antemano con el autoarranque desactivado —
p. ej. en Docker: `docker create --restart no ...` — atados a puertos de
loopback. El portero los arranca y los detiene; no los crea.

### Instalación manual

```bash
sudo mkdir -p /opt/gate-llm /etc/gate-llm
sudo cp gate_llm_proxy.py /opt/gate-llm/
sudo cp -r tools /opt/gate-llm/
sudo cp services.example.json /etc/gate-llm/services.json   # y edítala
sudo cp gate-llm.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gate-llm
python3 /opt/gate-llm/gate_llm_proxy.py --config /etc/gate-llm/services.json --check
```

## Gestionar los servicios: el menú

```bash
python3 /opt/gate-llm/tools/menu.py --config /etc/gate-llm/services.json
```

El menú edita el mismo JSON que lee el portero — una sola fuente de
verdad. Lista cada servicio y modelo con su estado en vivo y su VRAM,
cambia el modelo activo de un servicio (deteniéndolo y despertándolo
limpiamente), registra servicios y modelos nuevos, despierta/duerme a
mano y ajusta las ventanas de inactividad. Cada guardado se valida antes
(esquema, puertos duplicados, una sonda de enlace en los puertos nuevos
y una comprobación de salud cuando el servicio está despierto) y se
escribe de forma atómica con un `.bak`. Los cambios de ejecución
(ventana, modelo activo) los recoge el portero en marcha en segundos;
los estructurales (servicios añadidos/quitados, puertos) necesitan un
reinicio, y el menú lo dice.

## Verificar y medir tu máquina

```bash
# Ciclo completo sin atender: dormido -> petición -> despierto -> responde -> ocioso -> dormido
GATE_URL=http://127.0.0.1:8080 REQUEST_PATH=/v1/models \
  /opt/gate-llm/tools/verify_cycle.sh

# Tiempo al primer token + tokens/segundo contra cualquier endpoint compatible con OpenAI
python3 /opt/gate-llm/tools/bench.py --base-url http://127.0.0.1:8080 --model tu-modelo
```

Córrelos y deberías encontrar en tu hardware lo que este README afirma
en el nuestro — ese es el sentido de publicar los números con la receta.

## Referencia de configuración

Un solo archivo JSON (`services.example.json` es un ejemplo completo y
anotado):

| Campo | Significado |
|---|---|
| `name` | Id del servicio (logs, menú, endpoint de estado) |
| `listen_host` / `listen_port` | La cara pública de la que es dueño el portero |
| `backend_host` / `backend_port` | El backend real, solo en loopback |
| `health_url` | Contrato de preparación; el tráfico fluye solo cuando responde `health_expect_status` |
| `health_expect_status` | Estados HTTP esperados (por defecto `[200]`; un llama.cpp cargando responde 503) |
| `health_timeout_s` | Espera máxima de un despertar antes de que la petición retenida falle con 502 |
| `idle_timeout_s` | Ventana de inactividad antes de la parada; `0` = nunca dormir |
| `stop_grace_s` | Gracia concedida al backend al parar, antes de forzar |
| `start` / `stop` | Comandos como arreglos, p. ej. `docker start/stop` o `systemctl start/stop`. Omite `stop` y el portero es dueño del proceso arrancado y detiene su grupo de procesos |
| `models` + `active_model` | Opcional: varios pares arranque/parada (p. ej. dos tamaños de modelo) y cuál sirve. El menú los cambia |

## Limitaciones, dichas sin rodeos

* **La latencia en frío es real.** La primera petición tras un sueño
  paga el despertar (de segundos a decenas de segundos). Los clientes
  interactivos con timeouts cortos no lo tolerarán; los de lote y los
  tolerantes, sí. Elige la ventana por servicio en consecuencia.
* **Una GPU es una GPU.** Dos servicios despiertos a la vez deben seguir
  cabiendo juntos en la VRAM; el patrón ahorra memoria a lo largo del
  *tiempo*, no hace física nueva. La prioridad y la cola de despertares
  son trabajo futuro — ver [docs/MEJORAS.md](docs/MEJORAS.md).
* **Solo backends sin estado.** Parar debe ser seguro: un backend que
  guarda estado sin salvar en memoria no pertenece detrás de una parada
  por inactividad.
* **Una puerta compartida y ocupada duerme poco.** Si un servicio
  recibe tráfico casi continuo, el ahorro depende por completo de que la
  ventana sea más corta que los huecos naturales (ver la nota de la
  medición).
* El portero es un único proceso pequeño: si muere, systemd lo
  reinicia; los backends despiertos solo sirven a través de él, así que
  trátalo como cualquier otra pieza de la infraestructura que sirve.

## Documentación

* [Paper (EN, principal)](docs/PAPER.md) · [Paper (ES)](docs/PAPER.es.md) —
  diseño, metodología de medición, evidencia completa, limitaciones,
  trabajo relacionado.
* [Hoja de ruta de mejoras](docs/MEJORAS.md) — cola y prioridad de
  despertares, precalentamiento por horario, medición de «minutos
  despierto» (la base del cobro por uso), un modelo ligero de diálogo
  junto al grande, multi-GPU, un menú web y dónde encaja mejor la
  activación por socket de systemd.

## Licencia y autoría

MIT — ver [LICENSE](LICENSE). Úsalo, replícalo, mejóralo, y si publicas
tus números, di de dónde salió la receta.

Creado y medido por **Jeturing** — https://github.com/Jeturing/gate-llm
