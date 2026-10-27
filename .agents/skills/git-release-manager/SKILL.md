---
name: git-release-manager
description: >-
  Gestiona de forma autónoma y segura los commits, auditorías de privacidad/secretos,
  tags semánticos cada 3 fases y publicaciones en GitHub (origin/main) del Proyecto J.A.R.V.I.S.
---

# Skill: Git Release Manager (Proyecto J.A.R.V.I.S.)

Esta skill define el protocolo obligatorio que el agente debe seguir para llevar el control de versiones del repositorio J.A.R.V.I.S. de forma autónoma, segura y trazable.

## 1. Auditoría Previa de Privacidad y Seguridad (Obligatoria antes de cada commit)
1. Ejecutar `GitSafetyGuard` (`src/utils/git_guard.py`) sobre el árbol de trabajo.
2. Confirmar que `find_tracked_sensitive_files()` devuelva `[]`.
3. Verificar que `.env`, `.env.*` (salvo `.env.example`/`.env.template`), `prompts_history.txt`, `data/`, `*.log`, `*.bak` y cualquier archivo con datos personales o claves API estén excluidos en `.gitignore` y nunca se preparen en staging.
4. Escanear los archivos candidatos y `git diff --cached` con `scan_text_for_secrets()` confirmando `0` coincidencias.
5. Nunca usar `git add .` ni `git add -A`: preparar explícitamente cada archivo mediante `stage_specific_files([...])`.

## 2. Política de Commits y Tags cada 3 Fases
1. Dividir los planes de trabajo complejos en bloques de **3 fases verificadas**.
2. Al completar cada bloque de 3 fases (y tras pasar `py_compile` o las pruebas unitarias correspondientes):
   - Crear un commit descriptivo con formato Conventional Commits (`feat(...)`, `fix(...)`, `test(...)`, `docs(...)`).
   - Crear un **tag anotado de Git** (`git tag -a vX.Y.Z -m "..."`) que identifique el hito de esas 3 fases.
3. Publicar los commits y tags verificados en `origin/main` (`git push origin main` y `git push origin --tags`), sin usar jamás `--force`.
