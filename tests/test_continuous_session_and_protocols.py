"""
Suite de pruebas unitarias para la sesión conversacional continua ("Continuous Active Session"),
los Protocolos Stark y los efectos sonoros HUD de J.A.R.V.I.S.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from config.tools_schema import TOOLS_SCHEMA
from skills.system_skills import StarkProtocolSkill
from src.audio.sfx import (
    SoundFX,
    build_confirm_sound,
    build_standby_sound,
    build_wake_sound,
)
from src.brain.intent_router import IntentRouter
from src.session import (
    ActiveSession,
    SessionEnd,
    is_exit_command,
    is_meaningful,
    is_standby_command,
    normalize_text,
)
from src.tools.skill_manager import SkillManager


class TestContinuousActiveSession(unittest.TestCase):
    """
    Valida que ActiveSession implemente la sesión continua sin límite de turnos (eliminando
    el corte forzado a 3 mensajes) y retorne a reposo únicamente ante silencio o descanso.
    """

    def test_session_does_not_cut_at_three_messages(self):
        """Verifica que la sesión procese 10 turnos seguidos sin interrumpirse."""
        messages = [f"mensaje {i}" for i in range(10)]
        inputs = list(messages) + [None]  # Al final, silencio para salir
        spoken: list[str] = []

        def fake_listen():
            return inputs.pop(0) if inputs else None

        def fake_respond(text: str) -> str:
            return f"Respuesta a {text}"

        def fake_speak(text: str) -> None:
            spoken.append(text)

        session = ActiveSession(
            listen=fake_listen,
            respond=fake_respond,
            speak=fake_speak,
            post_speech_pause=0.0,
            sleep=lambda _: None,
        )

        result = session.run()
        self.assertEqual(result, SessionEnd.SILENCE)
        self.assertEqual(session.turn_count, 10, "La sesión debió procesar exactamente 10 turnos.")
        self.assertEqual(len(spoken), 10)

    def test_session_fifty_messages_continuity(self):
        """Verifica que la sesión pueda procesar 50 mensajes seguidos fluidamente."""
        inputs = [f"pregunta {i}" for i in range(50)] + ["descansa"]
        spoken: list[str] = []

        def fake_listen():
            return inputs.pop(0) if inputs else None

        session = ActiveSession(
            listen=fake_listen,
            respond=lambda t: f"R: {t}",
            speak=spoken.append,
            post_speech_pause=0.0,
            sleep=lambda _: None,
        )

        result = session.run()
        self.assertEqual(result, SessionEnd.STANDBY)
        self.assertEqual(session.turn_count, 50)
        # 50 respuestas de preguntas + 1 despedida de descanso
        self.assertEqual(len(spoken), 51)

    def test_session_returns_to_standby_on_silence_timeout(self):
        """Si el usuario no habla (timeout de inactividad de 7s -> None), vuelve a Reposo."""
        mock_sfx = MagicMock()
        session = ActiveSession(
            listen=lambda: None,
            respond=lambda t: "ok",
            speak=lambda t: None,
            sfx=mock_sfx,
            post_speech_pause=0.0,
            sleep=lambda _: None,
        )

        result = session.run()
        self.assertEqual(result, SessionEnd.SILENCE)
        self.assertEqual(session.turn_count, 0)
        mock_sfx.standby.assert_called_once()

    def test_session_returns_to_standby_on_rest_commands(self):
        """Comandos explícitos de descanso provocan respuesta breve y retorno a reposo."""
        rest_phrases = [
            "descansa",
            "modo reposo",
            "gracias jarvis es todo",
            "espera en segundo plano",
            "eso seria todo",
            "a dormir",
        ]

        for phrase in rest_phrases:
            with self.subTest(phrase=phrase):
                mock_sfx = MagicMock()
                spoken = []
                session = ActiveSession(
                    listen=lambda p=phrase: p,
                    respond=lambda t: "ok",
                    speak=spoken.append,
                    sfx=mock_sfx,
                    post_speech_pause=0.0,
                    sleep=lambda _: None,
                )
                result = session.run()
                self.assertEqual(result, SessionEnd.STANDBY)
                self.assertTrue(len(spoken) >= 1)
                self.assertIn("señor", spoken[0].lower())
                mock_sfx.standby.assert_called_once()

    def test_session_handles_exit_command(self):
        """Comandos de apagado finalizan el bucle con SessionEnd.EXIT."""
        spoken = []
        session = ActiveSession(
            listen=lambda: "apaga los sistemas",
            respond=lambda t: "ok",
            speak=spoken.append,
            post_speech_pause=0.0,
            sleep=lambda _: None,
        )

        result = session.run()
        self.assertEqual(result, SessionEnd.EXIT)
        self.assertTrue(any("apagando" in s.lower() for s in spoken))

    def test_session_aborts_on_external_flag(self):
        """Si la bandeja del sistema solicita cierre, aborta antes de escuchar."""
        session = ActiveSession(
            listen=lambda: "hola",
            respond=lambda t: "ok",
            speak=lambda t: None,
            should_abort=lambda: True,
        )
        result = session.run()
        self.assertEqual(result, SessionEnd.ABORT)


class TestStarkProtocols(unittest.TestCase):
    """
    Valida la implementación y registro de los Protocolos Stark (StarkProtocolSkill).
    """

    def setUp(self):
        self.skill = StarkProtocolSkill()

    def test_protocol_skill_registered_in_skill_manager(self):
        manager = SkillManager()
        self.assertIn("activate_protocol", manager.skills)
        manifest = manager.get_skills_manifest()
        names = [item["name"] for item in manifest]
        self.assertIn("activate_protocol", names)

    def test_protocol_skill_in_tools_schema(self):
        schema_names = [t["name"] for t in TOOLS_SCHEMA]
        self.assertIn("activate_protocol", schema_names)

    @patch("webbrowser.open")
    @patch("subprocess.Popen")
    def test_protocolo_trabajo(self, mock_popen, mock_browser):
        result = self.skill.execute(protocol="trabajo")
        self.assertIn("trabajo activado", result.lower())
        self.assertIn("procesador al", result.lower())
        self.assertIn("señor", result.lower())
        mock_popen.assert_called_once()
        mock_browser.assert_called_once()

    def test_protocolo_diagnostico(self):
        result = self.skill.execute(protocol="diagnostico")
        self.assertIn("diagnóstico completado", result.lower())
        self.assertIn("señor", result.lower())
        self.assertTrue("gpu" in result.lower() or "procesador" in result.lower())

    @patch("ctypes.windll.user32.keybd_event")
    def test_protocolo_silencio(self, mock_keybd):
        result = self.skill.execute(protocol="silencio")
        self.assertIn("silencio activado", result.lower())
        self.assertIn("señor", result.lower())
        self.assertTrue(mock_keybd.called)

    def test_protocolo_desconocido(self):
        result = self.skill.execute(protocol="protocolo_galactico_xyz")
        self.assertIn("no reconocido", result.lower())
        self.assertIn("señor", result.lower())


class TestSoundFX(unittest.TestCase):
    """
    Valida la síntesis en memoria de los efectos sonoros HUD.
    """

    def test_wav_synthesis_wake_sound(self):
        wav_bytes = build_wake_sound()
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes[:16])
        self.assertGreater(len(wav_bytes), 1000)

    def test_wav_synthesis_confirm_sound(self):
        wav_bytes = build_confirm_sound()
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes[:16])

    def test_wav_synthesis_standby_sound(self):
        wav_bytes = build_standby_sound()
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes[:16])

    def test_sfx_disabled_runs_silently(self):
        sfx = SoundFX(enabled=False)
        # No debe levantar ninguna excepción
        sfx.wake()
        sfx.confirm()
        sfx.standby()


class TestIntentRouterWithMCUProtocols(unittest.TestCase):
    """
    Valida el IntentRouter con la personalidad MCU y Function Calling para protocolos.
    """

    def test_intent_router_call_tool_for_protocol(self):
        mock_llm = MagicMock()
        mock_llm.generate_response.return_value = '{"action":"call_tool","tool_name":"activate_protocol","parameters":{"protocol":"trabajo"}}'
        router = IntentRouter(
            llm=mock_llm,
            tools=SkillManager().get_skills_manifest(),
        )
        decision = router.analyze_intent("inicia protocolo de trabajo")
        self.assertEqual(decision["action"], "call_tool")
        self.assertEqual(decision["tool_name"], "activate_protocol")
        self.assertEqual(decision["parameters"].get("protocol"), "trabajo")


if __name__ == "__main__":
    unittest.main()
