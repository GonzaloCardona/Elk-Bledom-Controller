# ELK-BLEDOM LED Controller (Windows)

Aplicación de escritorio ligera y completa para Windows para controlar tiras LED con controlador **ELK-BLEDOM** (y compatibles) directamente desde el PC a través de Bluetooth BLE (Bluetooth Low Energy).

> 📥 **[Descargar el ejecutable (.exe)](https://github.com/GonzaloCardona/Elk-Bledom-Controller/releases/latest)** — Listo para usar en Windows 10/11 sin necesidad de instalar Python.

---

## 🌟 Características

- 🔍 **Escaneo y conexión automática:** Detección de dispositivos BLE compatibles (`ELK-BLE`, `MELK`, `LEDBLE`, `JACKYLED`, etc.). Reconexión automática al último dispositivo recordado.
- ⏻ **Control de encendido y apagado.**
- 🎨 **Rueda de color HSV interactiva** y **deslizadores RGB** con vista previa en tiempo real.
- ☀️ **Control de brillo** (0–100%) y **velocidad**.
- ⚡ **Paleta de colores rápidos** (10 presets).
- 🌈 **20 modos de efectos dinámicos** (saltos de color, parpadeos, gradientes, etc.).
- 🎵 **Modo Discoteca / Música en Vivo:** Detección de bombos y beats en tiempo real vía WASAPI Loopback con colores neón puros al 100% (sin luz blanca). Varios estilos seleccionables (*Club / Beat Jump*, *Strobe / Destellos* y *Ecualizador Puro*), con control de sensibilidad y nivel de oscuridad de fondo.
- 💿 **Modo Portada (Álbum / Reproductor):** Extrae automáticamente la carátula de lo que estás escuchando en Windows (Spotify, YouTube, Tidal, Apple Music, VLC...) y tiñe la habitación con el color de la portada. Permite elegir entre color *Vibrante* (estilo Wallpaper Engine / acento vivo) o *Dominante* (color mayoritario en píxeles).
- 🖥️ **Modo Pantalla (Ambilight):** Captura de pantalla de ultra bajo retardo (con `mss`) que promedia los colores de la pantalla para iluminar la habitación en sincronía con juegos o películas.
- 🪟 **Integración con Bandeja del Sistema (System Tray):** Minimiza a la bandeja sin cerrarse, con opciones de control rápido en el menú contextual.
- 🚀 **Inicio con Windows:** Opción para arrancar minimizado automáticamente al iniciar sesión.
- ⚡ **Optimización de memoria RAM:** Destruye la interfaz gráfica al minimizarse a la bandeja y suspende los analizadores inactivos, reduciendo el consumo de RAM a niveles mínimos (~20-30 MB).

---

## 📋 Requisitos del Sistema

- **Sistema Operativo:** Windows 10 o Windows 11
- **Bluetooth:** Adaptador Bluetooth 4.0 o superior (BLE)
- **Python:** 3.8 o superior

---

## 🚀 Instalación y Uso

### Opción 1: Lanzador automático (Recomendado)

1. Haz doble clic en **`iniciar_elkbledom.bat`**.
2. El script comprobará Python, instalará automáticamente las dependencias si faltan e iniciará la aplicación.

### Opción 2: Manual con Pip

1. Clona este repositorio o descarga los archivos:
   ```bash
   git clone https://github.com/GonzaloCardona/Elk-Bledom-Controller.git
   cd Elk-Bledom-Controller
   ```

2. Instala las dependencias necesarias:
   ```bash
   pip install -r requirements.txt
   ```

3. Ejecuta la aplicación:
   ```bash
   python elkbledom_controller.py
   ```

---

## 📦 Compilación a Ejecutable (.exe)

Si deseas generar un archivo ejecutable independiente `.exe` sin necesidad de tener Python instalado:

1. Instala PyInstaller:
   ```bash
   pip install pyinstaller
   ```

2. Compila usando el archivo de especificación incluido:
   ```bash
   pyinstaller ELK-BLEDOM.spec
   ```

3. El ejecutable se generará en la carpeta `dist/ELK-BLEDOM.exe`.

---

## 🛠️ Dependencias

| Librería | Propósito |
|---|---|
| `bleak` | Comunicación Bluetooth Low Energy (BLE) |
| `pyaudiowpatch` | Captura de audio del sistema (WASAPI loopback) |
| `numpy` | Procesamiento numérico y FFT para el modo música |
| `mss` | Captura ultrarrápida de pantalla para modo pantalla |
| `pystray` | Icono y menú en la bandeja del sistema |
| `pillow` | Generación y manejo del icono de la bandeja y carátulas |
| `winrt-Windows.Media.Control` | Captura de carátulas del reproductor multimedia de Windows |

---

## 📡 Protocolo BLE (Referencia técnica)

El controlador opera enviando tramas de 9 bytes a las características de escritura UUID `0000fff3-0000-1000-8000-00805f9b34fb` o `0000ffe1-0000-1000-8000-00805f9b34fb`:

| Acción | Trama Hexadecimal (9 bytes) |
|---|---|
| Encender | `7e 00 04 01 00 00 00 00 ef` |
| Apagar | `7e 00 04 00 00 00 ff 00 ef` |
| Color RGB | `7e 00 05 03 RR GG BB 00 ef` |
| Brillo (0–100) | `7e 00 01 XX 00 00 00 00 ef` |
| Velocidad (0–100) | `7e 00 02 SS 00 00 00 00 ef` |
| Efecto | `7e 00 03 EE 03 00 00 00 ef` |

---

## 📄 Licencia

Proyecto de código abierto bajo licencia [MIT](LICENSE) (o de libre distribución).
