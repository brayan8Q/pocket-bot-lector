# Bot de Pocket Option (cuenta DEMO) — puente del Lector

Este es **el programa del bot**: se conecta a Pocket Option con la sesión de la cuenta **DEMO**, recibe los precios de todos los pares, arma las velas de 15 segundos y abre las operaciones que le manda el panel «Lector de tendencia».

El panel está en otro repositorio: `brayan8Q/senales-overlay` (carpeta `para-muse/` con las instrucciones y las fotos). Este repositorio es **solo el bot**.

Copia del 16 de noviembre de 2026, tal como corre en el equipo del dueño.

---

## Qué hace falta

- **Windows.** La sesión se guarda protegida con Windows (DPAPI) y el candado de «una sola copia» usa `msvcrt`. En Linux o Mac esas dos piezas hay que cambiarlas (`pocket_tyl/secret.py` y los usos de `msvcrt` en `engine.py`, `gpt.py`, `grabador.py`).
- **Python 3.11 o más nuevo.**
- Las librerías de `requirements.txt` (PySide6 para la ventana de inicio de sesión, Telethon para los módulos de señales de Telegram; el puente del Lector no usa Telegram, pero el paquete lo importa).

## Instalar

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Para correr las pruebas:

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
```

## Dos formas de entrar

| | Con la cuenta del dueño | **Sin cuenta: demo pública** |
|---|---|---|
| Qué es | Su cuenta DEMO de Pocket | La terminal de prueba pública de Pocket: dinero de juguete que no es de nadie |
| Contraseña | La escribe el dueño, una vez, en ese equipo | **No hace falta** |
| Acceso | `Conectar Pocket Option.bat` | **`Conectar demo publica (sin cuenta).bat`** |
| Dónde se ven las operaciones | En la cuenta DEMO del dueño | Solo en el bot y en el panel (esa demo no es de ninguna cuenta) |
| Para qué sirve | Operar la DEMO del dueño | Trabajar en **otro equipo** (por ejemplo el de Muse) sin tener su contraseña |

### Sin cuenta: demo pública (para trabajar en otro equipo)

1. Doble clic en **`Conectar demo publica (sin cuenta).bat`**. Abre la terminal de prueba pública de Pocket Option; no pide usuario ni contraseña.
2. En unos segundos la ventana dice «✅ Sesión de la cuenta DEMO capturada» y se cierra sola. (También hay un botón «Demo pública (sin cuenta)» en la ventana normal de conexión.)
3. Arrancar el bot con `Iniciar Pocket Lector.bat`.

**Estado: añadido el 16 de noviembre y sin probar contra Pocket** (esa noche el sitio no aceptaba conexiones desde el equipo del dueño). Lo comprobado: la ventana acepta y guarda una sesión DEMO anónima con el mismo formato que la de una cuenta. Lo que falta ver en vivo: que la página pública mande su sesión con `isDemo: 1` y que el bot conecte con ella. Si la ventana se queda en «Esperando…», mirar qué manda la página al abrir (`42["auth", …]`) y ajustar `handle_console` / `demo_check`.

Con la demo pública, Pocket no ofrece la vista de 4 gráficas. En el panel hay que encender «Usar las 4 gráficas del panel» (ajuste `multiWall: true`): el panel dibuja las cuatro con las velas del bot y los pares cambian solos.

## Iniciar sesión con la cuenta del dueño (una vez por equipo)

1. Doble clic en **`Conectar Pocket Option.bat`**. Abre una ventana con la página oficial de Pocket Option.
2. **El dueño escribe su usuario y contraseña** en esa página. El programa no los ve ni los guarda.
3. Pulsar «Abrir sala DEMO». La sesión de la cuenta DEMO se captura sola y se guarda protegida en `%LOCALAPPDATA%\PocketTyl`. La ventana se cierra.

La sesión guardada **no se puede copiar a otro equipo**: está cifrada para ese usuario de Windows. En cada equipo hay que iniciar sesión ahí mismo. Una sesión de cuenta REAL se ignora.

## Arrancar el bot

Doble clic en **`Iniciar Pocket Lector.bat`** (o `.venv\Scripts\python.exe -m pocket_tyl.lector`).

Abre una ventana «Pocket Lector DEMO» que va diciendo lo que hace. Al arrancar:

- Se conecta a la cuenta DEMO y sigue el precio de todos los activos abiertos.
- Graba los precios en `%LOCALAPPDATA%\PocketTyl\ticks_otc\<día>\<par>.csv`.
- Abre una puerta local en `127.0.0.1` y deja el puerto y una clave en `%LOCALAPPDATA%\PocketTyl\lector.json`. El panel lee ese archivo para hablarle.

Para detenerlo, cerrar esa ventana.

## Cómo le habla el panel

Todas las peticiones llevan la cabecera `X-Lector-Token` con la clave de `lector.json`.

| Ruta | Para qué | Respuesta |
|---|---|---|
| `GET /estado` | Saber si está conectado y si es DEMO | `connected`, `demo`, `balance`, montos |
| `GET /precios` | Precio de ahora de cada activo | `prices: [[símbolo, nombre, precio, edad s, pago, tipo]]` |
| `GET /velas?pares=A,B,C,D` | Velas de 15 s de hasta 8 pares | `pairs: [[símbolo, nombre, pago, cerradas [o,h,l,c], en curso, edad s, segundos abierta]]` |
| `POST /orden` | Abrir una operación | cuerpo `{ asset, dir: "up"|"down", seconds, level }` → `id`, precio y hora de entrada, pago, monto |
| `GET /resultado?id=` | Resultado de esa operación | `result: "win"|"loss"|"tie"`, `net`, precio de cierre |
| `GET /activos` | La fila de Pocket de cada activo (solo lectura) | `assets: [[id, símbolo, nombre, tipo, …, pago (5), …, es OTC (9), …, abierto (14), …, vencimiento mínimo en s (17), …]]` |

Montos por nivel: $1.00 / 2.20 / 4.84 / 10.65 / 23.43. El panel, tal como está configurado, usa siempre el nivel 0 ($1, sin martingala).

## Archivos

| Archivo | Qué es |
|---|---|
| `pocket_tyl/lector.py` | El puente: rutas, una operación a la vez, vigía de precios |
| `pocket_tyl/conector.py` | La conexión con Pocket (websocket): precios, abrir y cerrar operaciones |
| `pocket_tyl/velas.py` | Arma las velas de 15 s de cada par y las rellena con lo grabado |
| `pocket_tyl/grabador.py` | Graba los precios en disco |
| `pocket_tyl/capturar_ssid.py` | La ventana de inicio de sesión (tiene un puerto local de control en `127.0.0.1:9333`) |
| `pocket_tyl/secret.py` | Guarda y lee la sesión protegida; comprueba que sea DEMO |
| `pocket_tyl/ritmo.py` | Un seguidor de vela propio del bot. Apagado |
| Los demás | Pruebas anteriores (señales de Telegram, predictor). No se usan con el panel, pero el paquete los importa |
| `tests/` | Pruebas. `test_pocket_lector.py` y `test_pocket_ritmo.py` son las del puente |

## Reglas que no se rompen

- **Solo cuenta DEMO.** El puente se niega a arrancar en modo real y así debe seguir.
- **La contraseña la escribe el dueño**, en la página oficial, y no se guarda en ningún lado.
- **Un solo bot a la vez con la misma cuenta.** Si corre en dos equipos al mismo tiempo, los dos abren operaciones en la misma DEMO y los números ya no se pueden leer.
- **No subir a GitHub** la carpeta de datos, `lector.json`, la sesión ni registros. El `.gitignore` ya los deja fuera.
- **No abrir muchas páginas de Pocket a la vez ni recargar en bucle.** El 16 de noviembre, tras abrir cuatro terminales y recargar, el sitio dejó de aceptar conexiones desde el equipo durante un buen rato. Si una carga falla, se espera (1, 5, 15, 60 minutos) antes de reintentar.

## Cómo va el acierto

Para no prometer de más: con las reglas actuales del panel (confianza muy alta, $1 fijo, sin martingala) hay muy pocas operaciones todavía. Antes de esas reglas, 635 entradas en vivo dieron 50 % de acierto; con pago de 92 % hace falta 52.1 % para no perder. El plan es medir 200 entradas con las reglas nuevas antes de decidir nada.
