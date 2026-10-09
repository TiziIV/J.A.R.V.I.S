# Skills de J.A.R.V.I.S.

Esta carpeta contiene habilidades extensibles que ejecutan acciones solicitadas por el usuario. Se mantiene separada de `src/tools/`, que agrupa utilidades internas del sistema, y de `src/brain/`, que conversa con los modelos.

## Estructura actual

- `base_skill.py`: contrato abstracto común para habilidades.
- `system_skills.py`: contiene `TimeSkill`, `AppLauncherSkill`, `SystemInfoSkill` y `WebSearchSkill`.
- `__init__.py`: exporta el contrato público del paquete.

Las habilidades por defecto se registran en `src/tools/skill_manager.py`, que publica su manifiesto para que el enrutador pueda conocer los nombres, descripciones y argumentos aceptados.

## Añadir una habilidad

1. Crea un módulo con nombre descriptivo, por ejemplo `skills/weather_skill.py`.
2. Implementa `BaseSkill` y conserva `execute` como punto de entrada.
3. Añade type hints, docstrings y comentarios en español para las decisiones que no sean obvias.
4. Valida entradas y maneja errores esperables con resultados claros; nunca guardes credenciales dentro de la habilidad.
5. Si cambia el comportamiento general o la arquitectura, registra el avance en `CONTEXT.md`.

No muevas el paquete ni crees carpetas por anticipado: los imports actuales lo referencian desde la raíz del proyecto.
