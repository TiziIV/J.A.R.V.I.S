"""
Tests exhaustivos de todas las skills registradas en SkillManager.
Inyecta argumentos inválidos y verifica respuesta amigable sin excepciones.
"""

import unittest
from unittest.mock import MagicMock, patch
from skills.system_skills import (
    TimeSkill, AppLauncherSkill, SystemInfoSkill, WebSearchSkill
)
from skills.base_skill import BaseSkill
from src.tools.skill_manager import SkillManager


# ─── TimeSkill ────────────────────────────────────────────────────────────────

class TestTimeSkill(unittest.TestCase):

    def setUp(self):
        self.skill = TimeSkill()

    def test_retorna_string(self):
        result = self.skill.execute()
        self.assertIsInstance(result, str)

    def test_resultado_no_vacio(self):
        result = self.skill.execute()
        self.assertTrue(result.strip())

    def test_ignora_kwargs_extra(self):
        """Argumentos extra no deben causar TypeError."""
        result = self.skill.execute(foo="bar", numero=42, vacio=None)
        self.assertIsInstance(result, str)

    def test_ignora_kwargs_int(self):
        result = self.skill.execute(query=99999)
        self.assertIsInstance(result, str)

    def test_ignora_dict_vacio(self):
        result = self.skill.execute(**{})
        self.assertIsInstance(result, str)

    def test_contiene_hora_o_fecha(self):
        result = self.skill.execute()
        palabras_esperadas = ["hora", "Son las", "minutos", "enero", "febrero",
                              "marzo", "abril", "mayo", "junio", "julio",
                              "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        self.assertTrue(any(p.lower() in result.lower() for p in palabras_esperadas))

    def test_fallo_interno_devuelve_mensaje_amigable(self):
        """Si datetime.now() lanza excepción, devuelve mensaje amigable."""
        with patch("skills.system_skills.datetime") as mock_dt:
            mock_dt.now.side_effect = OSError("fallo de sistema")
            result = self.skill.execute()
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())


# ─── AppLauncherSkill ─────────────────────────────────────────────────────────

class TestAppLauncherSkill(unittest.TestCase):

    def setUp(self):
        self.skill = AppLauncherSkill()

    def test_app_valida_retorna_confirmacion(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            result = self.skill.execute(app_name="calculadora")
        self.assertIsInstance(result, str)
        self.assertIn("calculadora", result.lower())

    def test_app_inexistente_retorna_mensaje_amigable(self):
        result = self.skill.execute(app_name="photoshop_ultra_pro")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())
        self.assertNotIn("KeyError", result)
        self.assertNotIn("TypeError", result)

    def test_app_name_vacio_retorna_pedido_amigable(self):
        result = self.skill.execute(app_name="")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_app_name_none_no_crashea(self):
        """None en app_name → debe manejarse sin AttributeError."""
        result = self.skill.execute(app_name=None)
        self.assertIsInstance(result, str)

    def test_app_name_int_no_crashea(self):
        result = self.skill.execute(app_name=42)
        self.assertIsInstance(result, str)

    def test_app_name_dict_no_crashea(self):
        result = self.skill.execute(app_name={"key": "value"})
        self.assertIsInstance(result, str)

    def test_sin_kwargs_no_crashea(self):
        result = self.skill.execute()
        self.assertIsInstance(result, str)

    def test_subprocess_popen_lanza_excepcion(self):
        """Si Popen falla, devuelve mensaje amigable sin re-lanzar."""
        with patch("subprocess.Popen", side_effect=OSError("permiso denegado")):
            result = self.skill.execute(app_name="notepad")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_app_con_start_command(self):
        """Apps que usan 'start msedge' también se lanzan correctamente."""
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            result = self.skill.execute(app_name="edge")
        self.assertIsInstance(result, str)

    def test_kwargs_extra_ignorados(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            result = self.skill.execute(app_name="notepad", extra_param="valor", numero=7)
        self.assertIsInstance(result, str)


# ─── SystemInfoSkill ──────────────────────────────────────────────────────────

class TestSystemInfoSkill(unittest.TestCase):

    def setUp(self):
        self.skill = SystemInfoSkill()

    def test_retorna_string(self):
        result = self.skill.execute()
        self.assertIsInstance(result, str)

    def test_resultado_no_vacio(self):
        result = self.skill.execute()
        self.assertTrue(result.strip())

    def test_ignora_kwargs_extra(self):
        result = self.skill.execute(foo="bar", n=None)
        self.assertIsInstance(result, str)

    def test_fallo_psutil_retorna_mensaje_amigable(self):
        with patch("psutil.cpu_percent", side_effect=RuntimeError("fallo hw")):
            result = self.skill.execute()
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_contiene_cpu_o_ram(self):
        result = self.skill.execute()
        self.assertTrue("cpu" in result.lower() or "ram" in result.lower()
                        or "procesador" in result.lower() or "señor" in result.lower())

    def test_kwargs_con_tipos_erroneos(self):
        result = self.skill.execute(**{"query": [1, 2, 3], "platform": {"a": 1}})
        self.assertIsInstance(result, str)


# ─── WebSearchSkill ───────────────────────────────────────────────────────────

class TestWebSearchSkill(unittest.TestCase):

    def setUp(self):
        self.skill = WebSearchSkill()

    def test_busqueda_google_valida(self):
        with patch("webbrowser.open") as mock_open:
            result = self.skill.execute(query="música clásica")
        self.assertIsInstance(result, str)
        self.assertIn("música clásica", result)

    def test_busqueda_youtube(self):
        with patch("webbrowser.open") as mock_open:
            result = self.skill.execute(query="jazz", platform="youtube")
        self.assertIsInstance(result, str)
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        self.assertIn("youtube.com", url)

    def test_query_vacio_retorna_pedido_amigable(self):
        result = self.skill.execute(query="")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_query_none_no_crashea(self):
        result = self.skill.execute(query=None)
        self.assertIsInstance(result, str)

    def test_query_int_no_crashea(self):
        result = self.skill.execute(query=12345)
        self.assertIsInstance(result, str)

    def test_query_dict_no_crashea(self):
        result = self.skill.execute(query={"text": "hola"})
        self.assertIsInstance(result, str)

    def test_sin_kwargs_no_crashea(self):
        result = self.skill.execute()
        self.assertIsInstance(result, str)

    def test_platform_invalida_usa_google(self):
        with patch("webbrowser.open") as mock_open:
            result = self.skill.execute(query="test", platform="bing_ultra")
        url = mock_open.call_args[0][0]
        self.assertIn("google.com", url)

    def test_webbrowser_lanza_excepcion(self):
        with patch("webbrowser.open", side_effect=OSError("no hay browser")):
            result = self.skill.execute(query="algo")
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_caracteres_especiales_en_query(self):
        with patch("webbrowser.open") as mock_open:
            result = self.skill.execute(query="ñoño & cañón | español")
        self.assertIsInstance(result, str)

    def test_kwargs_extra_ignorados(self):
        with patch("webbrowser.open"):
            result = self.skill.execute(query="test", extra=True, numero=0)
        self.assertIsInstance(result, str)


# ─── SkillManager ─────────────────────────────────────────────────────────────

class TestSkillManager(unittest.TestCase):

    def setUp(self):
        self.manager = SkillManager()

    def test_skills_default_registradas(self):
        """Las 4 skills por defecto deben estar registradas."""
        for name in ("system_time", "app_launcher", "system_info", "web_search"):
            self.assertIn(name, self.manager.skills)

    def test_execute_skill_desconocida(self):
        result = self.manager.execute_skill("skill_magica")
        self.assertIsInstance(result, str)
        self.assertIn("señor", result.lower())

    def test_execute_skill_con_kwargs_invalidos(self):
        """Kwargs inválidos no deben propagarse como excepción al caller."""
        result = self.manager.execute_skill("system_time", argumento_invalido=999)
        self.assertIsInstance(result, str)

    def test_get_skills_manifest_retorna_lista(self):
        manifest = self.manager.get_skills_manifest()
        self.assertIsInstance(manifest, list)
        self.assertGreater(len(manifest), 0)

    def test_manifest_tiene_campos_requeridos(self):
        manifest = self.manager.get_skills_manifest()
        for item in manifest:
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("parameters", item)

    def test_register_skill_no_baseskill_no_crashea(self):
        """Registrar algo que no es BaseSkill no debe lanzar excepción."""
        self.manager.register_skill("esto no es una skill")  # type: ignore
        self.manager.register_skill(42)  # type: ignore
        self.manager.register_skill(None)  # type: ignore

    def test_register_skill_custom(self):
        """Una skill custom derivada de BaseSkill se registra correctamente."""
        class MiSkill(BaseSkill):
            def __init__(self):
                super().__init__("mi_skill", "Mi habilidad custom", {})
            def execute(self, **kwargs) -> str:
                return "resultado custom"

        self.manager.register_skill(MiSkill())
        self.assertIn("mi_skill", self.manager.skills)
        result = self.manager.execute_skill("mi_skill")
        self.assertEqual(result, "resultado custom")

    def test_execute_skill_que_lanza_excepcion_interna(self):
        """Si la skill interna explota, SkillManager captura y retorna mensaje amigable."""
        class BombaSkill(BaseSkill):
            def __init__(self):
                super().__init__("bomba", "Skill que explota", {})
            def execute(self, **kwargs) -> str:
                raise RuntimeError("BOOM intencional")

        self.manager.register_skill(BombaSkill())
        result = self.manager.execute_skill("bomba")
        self.assertIsInstance(result, str)
        self.assertNotIn("BOOM", result)

    def test_web_search_via_manager(self):
        with patch("webbrowser.open"):
            result = self.manager.execute_skill("web_search", query="python")
        self.assertIsInstance(result, str)

    def test_system_time_via_manager(self):
        result = self.manager.execute_skill("system_time")
        self.assertIsInstance(result, str)

    def test_app_launcher_via_manager_con_app_invalida(self):
        result = self.manager.execute_skill("app_launcher", app_name="app_inexistente_xyz")
        self.assertIsInstance(result, str)

    def test_app_launcher_via_manager_sin_kwargs(self):
        result = self.manager.execute_skill("app_launcher")
        self.assertIsInstance(result, str)


# ─── Windows Skills (Volume, Media, Clipboard, SystemPower) ────────────────────

from skills.windows_skills import (
    VolumeControlSkill, MediaControlSkill, ClipboardSkill, SystemPowerSkill
)


class TestVolumeControlSkill(unittest.TestCase):

    def setUp(self):
        self.skill = VolumeControlSkill()

    @patch("ctypes.windll.user32.keybd_event")
    def test_subir_volumen(self, mock_keybd):
        result = self.skill.execute(action="subir")
        self.assertIn("subido", result.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.keybd_event")
    def test_bajar_volumen(self, mock_keybd):
        result = self.skill.execute(action="bajar")
        self.assertIn("bajado", result.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.keybd_event")
    def test_silenciar_volumen(self, mock_keybd):
        result = self.skill.execute(action="silenciar")
        self.assertIn("silenciado", result.lower())
        self.assertTrue(mock_keybd.called)

    def test_accion_desconocida(self):
        result = self.skill.execute(action="volar")
        self.assertIn("No reconozco la acción", result)

    def test_action_vacio(self):
        result = self.skill.execute(action="")
        self.assertIn("Por favor indique", result)

    def test_action_none(self):
        result = self.skill.execute(action=None)
        self.assertIn("Por favor indique", result)

    def test_action_int(self):
        result = self.skill.execute(action=12345)
        self.assertIn("Por favor indique", result)

    def test_sin_kwargs(self):
        result = self.skill.execute()
        self.assertIn("Por favor indique", result)

    @patch("ctypes.windll.user32.keybd_event", side_effect=OSError("API de Windows falló"))
    def test_fallo_ctypes(self, mock_keybd):
        result = self.skill.execute(action="subir")
        self.assertIn("no pude controlar el volumen", result)


class TestMediaControlSkill(unittest.TestCase):

    def setUp(self):
        self.skill = MediaControlSkill()

    @patch("ctypes.windll.user32.keybd_event")
    def test_pausar_reproducir(self, mock_keybd):
        result = self.skill.execute(action="pausar")
        self.assertIn("alternada", result.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.keybd_event")
    def test_siguiente_cancion(self, mock_keybd):
        result = self.skill.execute(action="siguiente")
        self.assertIn("siguiente", result.lower())
        self.assertTrue(mock_keybd.called)

    @patch("ctypes.windll.user32.keybd_event")
    def test_cancion_anterior(self, mock_keybd):
        result = self.skill.execute(action="anterior")
        self.assertIn("anterior", result.lower())
        self.assertTrue(mock_keybd.called)

    def test_accion_desconocida(self):
        result = self.skill.execute(action="bailar")
        self.assertIn("No reconozco la acción multimedia", result)

    def test_action_vacio(self):
        result = self.skill.execute(action="")
        self.assertIn("Por favor indique", result)

    def test_action_none(self):
        result = self.skill.execute(action=None)
        self.assertIn("Por favor indique", result)

    def test_action_int(self):
        result = self.skill.execute(action=999)
        self.assertIn("Por favor indique", result)

    def test_sin_kwargs(self):
        result = self.skill.execute()
        self.assertIn("Por favor indique", result)

    @patch("ctypes.windll.user32.keybd_event", side_effect=RuntimeError("Error en Windows"))
    def test_fallo_ctypes(self, mock_keybd):
        result = self.skill.execute(action="pausar")
        self.assertIn("no pude controlar la reproducción", result)


class TestClipboardSkill(unittest.TestCase):

    def setUp(self):
        self.skill = ClipboardSkill()

    def test_action_vacio(self):
        result = self.skill.execute(action="")
        self.assertIn("Por favor indique si desea leer o copiar", result)

    def test_action_none(self):
        result = self.skill.execute(action=None)
        self.assertIn("Por favor indique si desea leer o copiar", result)

    def test_action_int(self):
        result = self.skill.execute(action=123)
        self.assertIn("Por favor indique si desea leer o copiar", result)

    def test_sin_kwargs(self):
        result = self.skill.execute()
        self.assertIn("Por favor indique", result)

    def test_accion_desconocida(self):
        result = self.skill.execute(action="imprimir")
        self.assertIn("No reconozco la acción", result)

    def test_copiar_sin_texto(self):
        result = self.skill.execute(action="copiar", text="")
        self.assertIn("No recibí texto para copiar", result)

    def test_copiar_texto_none(self):
        result = self.skill.execute(action="copiar", text=None)
        self.assertIn("No recibí texto para copiar", result)

    def test_copiar_texto_int(self):
        result = self.skill.execute(action="copiar", text=12345)
        self.assertIn("No recibí texto para copiar", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=0)
    def test_leer_open_clipboard_falla(self, mock_open):
        result = self.skill.execute(action="leer")
        self.assertIn("No pude abrir el portapapeles", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=1)
    @patch("ctypes.windll.user32.GetClipboardData", return_value=0)
    @patch("ctypes.windll.user32.CloseClipboard")
    def test_leer_clipboard_vacio(self, mock_close, mock_data, mock_open):
        result = self.skill.execute(action="leer")
        self.assertIn("vacío", result.lower())

    @patch("ctypes.windll.user32.OpenClipboard", return_value=1)
    @patch("ctypes.windll.user32.GetClipboardData", return_value=1234)
    @patch("ctypes.windll.kernel32.GlobalLock", return_value="Texto en portapapeles")
    @patch("ctypes.windll.kernel32.GlobalUnlock")
    @patch("ctypes.windll.user32.CloseClipboard")
    def test_leer_clipboard_con_exito(self, mock_close, mock_unlock, mock_lock, mock_data, mock_open):
        result = self.skill.execute(action="leer")
        self.assertIn("Texto en portapapeles", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=1)
    @patch("ctypes.windll.user32.GetClipboardData", return_value=1234)
    @patch("ctypes.windll.kernel32.GlobalLock", return_value=None)
    @patch("ctypes.windll.user32.CloseClipboard")
    def test_leer_globallock_nulo(self, mock_close, mock_lock, mock_data, mock_open):
        result = self.skill.execute(action="leer")
        self.assertIn("No pude leer el contenido", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=0)
    def test_escribir_open_clipboard_falla(self, mock_open):
        result = self.skill.execute(action="copiar", text="Hola mundo")
        self.assertIn("No pude abrir el portapapeles", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=1)
    @patch("ctypes.windll.user32.EmptyClipboard")
    @patch("ctypes.windll.kernel32.GlobalAlloc", return_value=0)
    @patch("ctypes.windll.user32.CloseClipboard")
    def test_escribir_globalalloc_falla(self, mock_close, mock_alloc, mock_empty, mock_open):
        result = self.skill.execute(action="copiar", text="Hola mundo")
        self.assertIn("No pude reservar memoria", result)

    @patch("ctypes.windll.user32.OpenClipboard", return_value=1)
    @patch("ctypes.windll.user32.EmptyClipboard")
    @patch("ctypes.windll.kernel32.GlobalAlloc", return_value=9999)
    @patch("ctypes.windll.kernel32.GlobalLock", return_value=123456)
    @patch("ctypes.memmove")
    @patch("ctypes.windll.kernel32.GlobalUnlock")
    @patch("ctypes.windll.user32.SetClipboardData")
    @patch("ctypes.windll.user32.CloseClipboard")
    def test_escribir_exito(self, mock_close, mock_set, mock_unlock, mock_memmove, mock_lock, mock_alloc, mock_empty, mock_open):
        result = self.skill.execute(action="copiar", text="Texto guardado")
        self.assertIn("copiado al portapapeles", result.lower())


class TestSystemPowerSkill(unittest.TestCase):

    def setUp(self):
        self.skill = SystemPowerSkill()

    def test_action_vacio(self):
        result = self.skill.execute(action="")
        self.assertIn("Por favor indique", result)

    def test_action_none(self):
        result = self.skill.execute(action=None)
        self.assertIn("Por favor indique", result)

    def test_action_int(self):
        result = self.skill.execute(action=999)
        self.assertIn("Por favor indique", result)

    def test_sin_kwargs(self):
        result = self.skill.execute()
        self.assertIn("Por favor indique", result)

    def test_accion_desconocida(self):
        result = self.skill.execute(action="reiniciar_bios")
        self.assertIn("No reconozco la acción", result)

    @patch("ctypes.windll.user32.LockWorkStation", return_value=1)
    def test_bloquear_pantalla_exito(self, mock_lock):
        result = self.skill.execute(action="bloquear")
        self.assertIn("bloqueada", result.lower())

    @patch("ctypes.windll.user32.LockWorkStation", return_value=0)
    def test_bloquear_pantalla_fallo(self, mock_lock):
        result = self.skill.execute(action="bloquear")
        self.assertIn("privilegios elevados", result.lower())

    @patch("subprocess.run")
    def test_cancelar_apagado_exito(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        result = self.skill.execute(action="cancelar_apagado")
        self.assertIn("cancelado exitosamente", result.lower())

    @patch("subprocess.run")
    def test_cancelar_apagado_sin_shutdown_activo(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_run.return_value = mock_proc
        result = self.skill.execute(action="cancelar_apagado")
        self.assertIn("ningún apagado programado activo", result.lower())

    @patch("ctypes.windll.user32.LockWorkStation", side_effect=OSError("Error de sistema"))
    def test_excepcion_bloquear(self, mock_lock):
        result = self.skill.execute(action="bloquear")
        self.assertIn("no pude ejecutar la acción", result.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
