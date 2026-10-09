# Guía de desarrollo de J.A.R.V.I.S.

Estas reglas aplican a todo el repositorio. Antes de editar, revisa este archivo, `CONTEXT.md` y la documentación del módulo afectado. Conserva las decisiones y límites ya validados en el proyecto.

## Objetivo y estilo

- Construye un asistente local de voz modular, claro y eficiente, apto para Windows y una GTX 1660 con 6 GB de VRAM.
- Escribe código Python con type hints y nombres claros. Explica en español los bloques importantes y el motivo de parámetros técnicos para que una persona principiante pueda seguir el flujo.
- Prefiere cambios pequeños que mantengan las interfaces existentes; actualiza las llamadas cuando una interfaz necesariamente cambie.
- No afirmes que un comportamiento está probado si no se ejecutó la verificación correspondiente.

## Límites de arquitectura

- `src/audio/recorder.py`: captura de micrófono y guardado de audio. No debe contener lógica de LLM ni TTS.
- `src/audio/stt.py`: transcripción de archivos. Mantén Whisper `base`, CUDA y `float16` como valores predeterminados configurables; agrega fallback controlado cuando la GPU no esté disponible.
- `src/audio/tts.py`: síntesis y reproducción. Ejecuta tareas lentas fuera del hilo conversacional y limpia temporales incluso ante errores.
- `src/brain/`: adaptadores de modelos. El cerebro local Ollama es primario; Gemini es respaldo. Ambos reciben el mismo prompt desde `config/prompts.py`.
- `skills/`: contratos y habilidades de usuario extensibles. `src/tools/` contiene herramientas internas del sistema. No mezcles estas responsabilidades ni muevas módulos sin actualizar imports y documentación.
- `config/settings.py`: carga `.env`, constantes de entorno y ajustes de plataforma. Nunca incluyas secretos en código, logs, documentación o historial versionado.
- `main.py`: conecta módulos y controla el ciclo de conversación; evita implementar allí detalles de captura, modelos o reproducción.

## Audio, recursos y resiliencia

- Mantén audio mono a 16 kHz porque es el formato esperado por Whisper y evita remuestreos innecesarios.
- No cargues modelos de voz pesados simultáneamente en GPU; libera recursos antes de cambiar modelos.
- Ajusta VAD con margen de silencio y pre captura, procurando tolerar pausas naturales sin grabaciones indefinidas. Mantén un límite máximo de duración.
- Trata transcripciones con métricas de Whisper y controles mínimos de texto antes de llamar a un modelo. Los comandos breves válidos, como “sí” o “no”, deben seguir permitidos.
- Captura errores de periféricos, CUDA/DLL, red y reproductor con mensajes comprensibles. Un fallo de un turno no debe cerrar el ciclo completo.
- Usa archivos temporales cerrados antes de escribirlos. En Windows, reintenta brevemente la eliminación cuando un reproductor aún mantenga el archivo bloqueado.
- TTS debe correr fuera del hilo que controla el asistente. No abras el micrófono sobre la reproducción anterior para prevenir eco.

## Respuestas y prompts

- Mantén una identidad y estilo comunes para Ollama y Gemini mediante `config/prompts.py`.
- Para voz, responde por defecto en una o dos oraciones breves, naturales y directas. Si el usuario pide detalle, respeta su pedido.
- Conserva los límites de generación configurados en cada proveedor y evita añadir instrucciones contradictorias en cada cliente.

## Organización de skills

- Toda habilidad concreta debe derivar de `skills.base_skill.BaseSkill`, tener un nombre y descripción claros, y documentar entradas, resultado y errores esperables.
- Mantén cada habilidad en un módulo pequeño y sin dependencias con la captura de audio o los clientes LLM salvo que su función lo requiera.
- No agregues módulos vacíos ni muevas `skills/` a `src/` sin una necesidad real: los imports actuales usan `skills` como paquete de primer nivel.
- Consulta `skills/README.md` antes de crear una habilidad nueva.

## Seguridad y configuración

- Lee claves y opciones de despliegue desde `.env` a través de `config/settings.py`; `.env` y `prompts_history.txt` no se versionan.
- Valida la ausencia de claves con un modo degradado comprensible. No imprimas la clave ni el contenido de `.env`.
- Usa dependencias ya presentes en `requirements.txt` siempre que cubran la necesidad.

## Bitácoras obligatorias

- Al iniciar cada cambio, agrega el prompt exacto del usuario a `prompts_history.txt` con separadores y fecha/paso. El archivo está excluido de Git.
- Después de cada cambio relevante, actualiza `CONTEXT.md` con estado de archivos, verificaciones realmente ejecutadas y resultados, problemas resueltos y pendientes.
- Si no se ejecutaron pruebas o comprobaciones, indícalo explícitamente en `CONTEXT.md`.

## Verificación

- Ejecuta comprobaciones proporcionadas por el proyecto o verificaciones estáticas pequeñas cuando estén disponibles y sean relevantes.
- Para cambios de audio, informa que el hardware físico requiere prueba manual si no se pudo probar micrófono o reproducción en esta sesión.
- Revisa el diff final para detectar cambios ajenos, secretos o archivos temporales.
