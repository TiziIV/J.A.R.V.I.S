import threading
from typing import Callable, Optional
from PIL import Image, ImageDraw
import pystray


class JarvisTray:
    """
    Controlador de la interfaz en segundo plano para la bandeja del sistema (System Tray).
    Muestra un icono dinámico que cambia de color según el estado del asistente
    (Azul = reposo, Rojo = hablando, Verde = escuchando) y ofrece un menú contextual
    para interactuar rápidamente sin necesidad de consola.
    """

    # Mapeo de colores RGB según el estado del sistema
    COLOR_MAP = {
        "reposo": (30, 144, 255),       # Azul clásico / DodgerBlue
        "idle": (30, 144, 255),
        "hablando": (220, 20, 60),      # Rojo / Crimson
        "speaking": (220, 20, 60),
        "escuchando": (46, 204, 113),    # Verde / Emerald
        "listening": (46, 204, 113),
        "procesando": (241, 196, 15),   # Amarillo / SunFlower
        "processing": (241, 196, 15),
    }

    def __init__(
        self,
        on_status: Optional[Callable[[], None]] = None,
        on_clear_memory: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Inicializa el System Tray de J.A.R.V.I.S.
        
        Args:
            on_status: Función opcional invocada al pulsar 'Ver Estado'.
            on_clear_memory: Función opcional invocada al pulsar 'Limpiar Memoria'.
            on_exit: Función opcional invocada al pulsar 'Salir'.
        """
        self._on_status = on_status
        self._on_clear_memory = on_clear_memory
        self._on_exit = on_exit

        self._current_state = "reposo"
        self._exit_requested = threading.Event()
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False

        # Creamos la imagen inicial del ícono en estado reposo (azul)
        initial_image = self._create_circle_image(self.COLOR_MAP["reposo"])

        # Definimos el menú contextual con las tres opciones requeridas
        menu = pystray.Menu(
            pystray.MenuItem("Ver Estado", self._handle_status),
            pystray.MenuItem("Limpiar Memoria", self._handle_clear_memory),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", self._handle_exit),
        )

        # Instanciamos el objeto Icon de pystray
        self._icon = pystray.Icon(
            name="JARVIS",
            icon=initial_image,
            title="J.A.R.V.I.S. (Reposo)",
            menu=menu,
        )

    @staticmethod
    def _create_circle_image(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> Image.Image:
        """
        Genera dinámicamente un ícono circular en memoria usando Pillow (PIL).
        
        Args:
            color: Tupla RGB con el color de relleno del círculo.
            size: Dimensiones del lienzo en píxeles.
            
        Returns:
            Image.Image: Imagen RGBA con canal alfa transparente.
        """
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Dibujamos un círculo con un borde exterior suave
        draw.ellipse([4, 4, size[0] - 5, size[1] - 5], fill=color, outline=(255, 255, 255, 220), width=3)
        return image

    @property
    def is_exit_requested(self) -> bool:
        """Indica si el usuario solicitó la salida del sistema desde el menú del tray."""
        return self._exit_requested.is_set()

    @property
    def current_state(self) -> str:
        """Devuelve el estado visual actual del tray."""
        return self._current_state

    def set_state(self, state: str) -> None:
        """
        Actualiza dinámicamente el estado y el color del ícono en la barra de tareas.
        
        Args:
            state: Nombre del estado ("reposo", "hablando", "escuchando", "procesando").
        """
        self._current_state = state.lower()
        color = self.COLOR_MAP.get(self._current_state, self.COLOR_MAP["reposo"])
        new_image = self._create_circle_image(color)

        if self._icon is not None:
            try:
                self._icon.icon = new_image
                self._icon.title = f"J.A.R.V.I.S. ({state.capitalize()})"
            except Exception as err:
                print(f"[Tray] Advertencia al actualizar estado: {err}")

    def _handle_status(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Manejador del evento 'Ver Estado'."""
        print(f"\n[Tray] Estado actual del sistema: {self._current_state.upper()}")
        try:
            icon.notify(
                f"Estado: {self._current_state.capitalize()}",
                "J.A.R.V.I.S. Asistente Local",
            )
        except Exception:
            pass
        if self._on_status:
            try:
                self._on_status()
            except Exception as err:
                print(f"[Tray Error] Falló callback de estado: {err}")

    def _handle_clear_memory(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Manejador del evento 'Limpiar Memoria'."""
        print("\n[Tray] Solicitud de reinicio de memoria conversacional.")
        try:
            icon.notify(
                "Memoria conversacional reiniciada.",
                "J.A.R.V.I.S. Asistente Local",
            )
        except Exception:
            pass
        if self._on_clear_memory:
            try:
                self._on_clear_memory()
            except Exception as err:
                print(f"[Tray Error] Falló callback de memoria: {err}")

    def _handle_exit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Manejador del evento 'Salir'."""
        print("\n[Tray] Salida solicitada desde la bandeja del sistema.")
        self._exit_requested.set()
        if self._on_exit:
            try:
                self._on_exit()
            except Exception as err:
                print(f"[Tray Error] Falló callback de salida: {err}")
        self.stop()

    def _run(self) -> None:
        """Bucle interno de ejecución de pystray en hilo dedicado."""
        try:
            if self._icon:
                self._icon.run()
        except Exception as err:
            print(f"[Tray Error] Ocurrió un fallo en el hilo de la bandeja: {err}")
        finally:
            self._is_running = False

    def start(self) -> None:
        """Inicia el icono en la bandeja del sistema en un hilo secundario independiente."""
        if self._is_running:
            return
        self._is_running = True
        self._thread = threading.Thread(target=self._run, name="JarvisTrayThread", daemon=True)
        self._thread.start()
        print("[Tray] Icono en bandeja del sistema iniciado en segundo plano.")

    def stop(self) -> None:
        """Detiene y remueve el ícono de la bandeja de tareas."""
        if not self._is_running:
            return
        self._is_running = False
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        print("[Tray] Icono de la bandeja del sistema detenido.")

