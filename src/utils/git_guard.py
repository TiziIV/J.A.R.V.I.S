"""
Guardia de seguridad de Git (`GitSafetyGuard`) para el Proyecto J.A.R.V.I.S.

Responsabilidades principales:
1. Inspeccionar el estado del repositorio (`git status --short --branch`, rama actual, remotos,
   archivos modificados, archivos sin seguimiento y archivos preparados en staging) sin usar `shell=True`.
2. Distinguir y proteger los cambios preexistentes en el árbol de trabajo frente a los cambios
   generados por una tarea de programación (por ejemplo, tras ejecutar Codex CLI).
3. Evitar la inclusión de archivos sensibles (`.env`, `.env.*`, `prompts_history.txt`, `data/`,
   `*.log`, claves privadas) tanto en `git add` como mediante detección en `git ls-files` y escaneo
   de secretos en `git diff`.
4. Separar estrictamente los permisos de **edición**, **commit** y **push**, prohibiendo `git add .`
   indiscriminado, `git push --force` y comandos destructivos (`git reset --hard`, `git clean -fd`,
   `git checkout -- .`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Optional, Sequence

from config.settings import BASE_DIR
from src.brain.exceptions import redact_secrets


# Nombres de archivos de plantilla seguros que SÍ pueden versionarse aunque empiecen con `.env`.
SAFE_ENV_TEMPLATES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.template",
        ".env.sample",
        ".env.dist",
    }
)

# Archivos exactos considerados sensibles o locales en J.A.R.V.I.S.
SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        "prompts_history.txt",
        "credentials.json",
        "token.json",
        "auth.json",
        "id_rsa",
        "id_ed25519",
    }
)

# Extensiones sensibles o artefactos locales que nunca deben incluirse en commits.
SENSITIVE_EXTENSIONS: tuple[str, ...] = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".log",
    ".log.txt",
    ".wav",
    ".mp3",
)

# Patrones para detectar valores reales de claves API o tokens dentro de un diff de Git.
_DIFF_SECRET_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Clave Anthropic (sk-ant-...)", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("Clave OpenAI (sk-...)", re.compile(r"sk-[A-Za-z0-9_\-]{12,}")),
    ("Clave Google AI (AIza...)", re.compile(r"AIza[0-9A-Za-z_\-]{12,}")),
    ("Clave Google (AQ....)", re.compile(r"AQ\.[0-9A-Za-z_\-]{16,}")),
    ("Token JWT (eyJ...)", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "Asignación de clave API en texto plano",
        re.compile(
            r"(?im)^\+?\s*(?:GEMINI_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*['\"]?(?!tu_|your_|<|\$\{|\[REDACTED\]|''|\"\")[A-Za-z0-9_\-\.]{10,}"
        ),
    ),
)

# Comandos de Git destructivos o prohibidos por política de seguridad.
FORBIDDEN_GIT_FLAGS: frozenset[str] = frozenset(
    {
        "--force",
        "-f",
        "--force-with-lease",
        "--hard",
    }
)


def _normalize_rel_path(path_str: str) -> str:
    """Normaliza una ruta relativa al formato POSIX sin barras iniciales para comparaciones uniformes."""
    cleaned = (path_str or "").strip().strip('"').replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def is_sensitive_path(rel_path: str) -> bool:
    """
    Determina si una ruta relativa corresponde a un archivo sensible, de credenciales,
    datos locales o logs que jamás debe incluirse en un commit de Git.
    """
    norm = _normalize_rel_path(rel_path)
    if not norm:
        return False

    posix = PurePosixPath(norm)
    name_lower = posix.name.lower()
    parts_lower = [part.lower() for part in posix.parts]

    if name_lower in SAFE_ENV_TEMPLATES:
        return False

    if name_lower in SENSITIVE_FILENAMES:
        return True

    if name_lower.startswith(".env.") or name_lower.endswith(".env"):
        return True

    if any(name_lower.endswith(ext) for ext in SENSITIVE_EXTENSIONS):
        return True

    if name_lower.startswith("client_secret") and name_lower.endswith(".json"):
        return True

    # Directorios sensibles o locales del proyecto
    if any(part in ("data", ".codex", "venv", ".venv") for part in parts_lower[:-1]):
        return True
    if parts_lower and parts_lower[0] in ("data", ".codex", "venv", ".venv"):
        return True

    return False


def scan_text_for_secrets(text: str) -> list[str]:
    """
    Analiza el contenido de un archivo o diff buscando posibles secretos reales.
    Devuelve una lista de descripciones legibles sin exponer jamás el valor del secreto.
    """
    if not text:
        return []
    findings: list[str] = []
    for label, pattern in _DIFF_SECRET_DETECTORS:
        if pattern.search(text):
            findings.append(label)
    return findings


@dataclass(frozen=True)
class GitTreeSnapshot:
    """Instantánea inmutable del estado del repositorio Git en un instante dado."""

    branch: str
    root_dir: Path
    remotes: dict[str, str]
    modified_files: tuple[str, ...]
    untracked_files: tuple[str, ...]
    staged_files: tuple[str, ...]
    tracked_files: tuple[str, ...]
    raw_status: str

    @property
    def all_dirty_files(self) -> set[str]:
        """Conjunto de todos los archivos modificados, preparados o sin seguimiento."""
        return set(self.modified_files) | set(self.untracked_files) | set(self.staged_files)


class GitSafetyGuard:
    """
    Gestor de seguridad de Git que protege cambios preexistentes, impide publicar secretos
    y separa estrictamente edición, commit y push.
    """

    def __init__(
        self,
        repo_dir: str | Path = BASE_DIR,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.repo_dir: Path = Path(repo_dir).resolve()
        self.timeout: float = max(float(timeout), 2.0)

    def _run_git(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """
        Ejecuta un comando `git` de forma segura sin `shell=True`.
        Rechaza banderas destructivas o de forzado.
        """
        for token in args:
            if token in FORBIDDEN_GIT_FLAGS:
                raise PermissionError(
                    f"Operación de Git bloqueada por política de seguridad: bandera prohibida '{token}'."
                )

        cmd = ["git", *args]
        return subprocess.run(
            cmd,
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            shell=False,
            check=False,
        )

    def inspect_state(self) -> GitTreeSnapshot:
        """
        Inspecciona el estado actual del repositorio Git (`status`, rama, raíz, remotos,
        archivos en staging y archivos rastreados).
        """
        status_proc = self._run_git(["status", "--short", "--branch"])
        raw_status = (status_proc.stdout or "").strip()

        branch = "HEAD"
        modified: list[str] = []
        untracked: list[str] = []
        staged: list[str] = []

        for line in (status_proc.stdout or "").splitlines():
            if not line.strip():
                continue
            if line.startswith("## "):
                header = line[3:].strip()
                branch = header.split("...")[0].split()[0].strip()
                continue
            if len(line) < 4:
                continue
            index_state = line[0]
            worktree_state = line[1]
            raw_path = line[3:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1].strip()
            norm_path = _normalize_rel_path(raw_path)

            if index_state == "?" and worktree_state == "?":
                untracked.append(norm_path)
            else:
                if index_state not in (" ", "?"):
                    staged.append(norm_path)
                if worktree_state not in (" ", "?"):
                    modified.append(norm_path)

        # Raíz del repositorio
        root_proc = self._run_git(["rev-parse", "--show-toplevel"])
        root_dir = (
            Path(root_proc.stdout.strip()).resolve()
            if root_proc.returncode == 0 and root_proc.stdout.strip()
            else self.repo_dir
        )

        # Remotos configurados
        remotes: dict[str, str] = {}
        remote_proc = self._run_git(["remote", "-v"])
        if remote_proc.returncode == 0:
            for rline in (remote_proc.stdout or "").splitlines():
                parts = rline.split()
                if len(parts) >= 2:
                    remotes[parts[0].strip()] = parts[1].strip()

        # Archivos preparados según `git diff --cached --name-only`
        staged_proc = self._run_git(["diff", "--cached", "--name-only"])
        if staged_proc.returncode == 0:
            for sline in (staged_proc.stdout or "").splitlines():
                norm_s = _normalize_rel_path(sline)
                if norm_s and norm_s not in staged:
                    staged.append(norm_s)

        # Archivos versionados en el índice (`git ls-files`)
        tracked: list[str] = []
        ls_proc = self._run_git(["ls-files"])
        if ls_proc.returncode == 0:
            for tline in (ls_proc.stdout or "").splitlines():
                norm_t = _normalize_rel_path(tline)
                if norm_t:
                    tracked.append(norm_t)

        return GitTreeSnapshot(
            branch=branch,
            root_dir=root_dir,
            remotes=remotes,
            modified_files=tuple(modified),
            untracked_files=tuple(untracked),
            staged_files=tuple(staged),
            tracked_files=tuple(tracked),
            raw_status=raw_status,
        )

    def find_tracked_sensitive_files(self, snapshot: Optional[GitTreeSnapshot] = None) -> list[str]:
        """
        Utiliza `git ls-files` para detectar si algún archivo sensible ya está versionado en Git
        (dado que `.gitignore` no des-versiona archivos previamente registrados).
        """
        snap = snapshot or self.inspect_state()
        return [path for path in snap.tracked_files if is_sensitive_path(path)]

    def detect_task_changes(
        self,
        baseline: GitTreeSnapshot,
        current: Optional[GitTreeSnapshot] = None,
    ) -> tuple[list[str], list[str]]:
        """
        Compara una instantánea previa (`baseline`) con el estado actual (`current`) y separa:
        - `task_only_files`: archivos que estaban limpios antes de la tarea y fueron creados/modificados ahora.
        - `preexisting_files`: archivos que ya tenían cambios sin confirmar antes de iniciar la tarea.
        """
        now = current or self.inspect_state()
        preexisting = sorted(baseline.all_dirty_files)
        preexisting_set = set(preexisting)
        task_only = sorted(f for f in now.all_dirty_files if f not in preexisting_set)
        return task_only, preexisting

    def get_diff_summary(self, files: Optional[Sequence[str]] = None, *, cached: bool = False) -> str:
        """Obtiene el diff sanitizado del árbol de trabajo o del área de preparación (`--cached`)."""
        args: list[str] = ["diff"]
        if cached:
            args.append("--cached")
        if files:
            args.append("--")
            args.extend(_normalize_rel_path(f) for f in files if _normalize_rel_path(f))
        proc = self._run_git(args)
        return redact_secrets(proc.stdout or "")

    def validate_commit_candidates(
        self,
        candidate_files: Sequence[str],
        *,
        baseline: Optional[GitTreeSnapshot] = None,
    ) -> tuple[bool, str]:
        """
        Verifica que una lista de archivos candidatos para commit sea segura:
        1. No permite comodines ni `.` (`git add .` está prohibido).
        2. Rechaza archivos sensibles (`.env`, `prompts_history.txt`, `data/`, `*.log`, etc.).
        3. Rechaza archivos que ya tenían modificaciones preexistentes no confirmadas en `baseline`.
        4. Verifica que ningún archivo sensible esté versionado en el índice (`git ls-files`).
        5. Escanea el `git diff` de los archivos candidatos buscando claves API o tokens.
        """
        if not candidate_files:
            return False, "No se especificaron archivos para preparar el commit."

        normalized_files: list[str] = []
        for raw_f in candidate_files:
            cleaned = (raw_f or "").strip()
            if cleaned in (".", "*", "-A", "--all"):
                return (
                    False,
                    f"No se permite preparar archivos de forma indiscriminada ('{cleaned}'). "
                    "Indique los archivos específicos de la tarea.",
                )
            norm = _normalize_rel_path(cleaned)
            if not norm:
                continue
            if is_sensitive_path(norm):
                return (
                    False,
                    f"Archivo sensible bloqueado por política de seguridad: '{norm}' no puede incluirse en Git.",
                )
            normalized_files.append(norm)

        if not normalized_files:
            return False, "La lista de archivos normalizados está vacía."

        if baseline is not None:
            overlapping = sorted(set(normalized_files) & baseline.all_dirty_files)
            if overlapping:
                return (
                    False,
                    "Se detectaron cambios preexistentes sin confirmar en: "
                    f"{', '.join(overlapping)}. Se detiene la preparación para no mezclar ni sobrescribir trabajo previo.",
                )

        tracked_sensitive = self.find_tracked_sensitive_files()
        if tracked_sensitive:
            return (
                False,
                "Riesgo de seguridad: se detectaron archivos sensibles ya versionados en Git "
                f"({', '.join(tracked_sensitive)}). Se detiene la operación hasta que el usuario autorice su limpieza.",
            )

        diff_proc = self._run_git(["diff", "--", *normalized_files])
        secret_hits = scan_text_for_secrets(diff_proc.stdout or "")
        if secret_hits:
            return (
                False,
                "Se detectaron posibles secretos o claves API en los cambios ("
                f"{', '.join(secret_hits)}). Commit bloqueado por seguridad.",
            )

        return True, "Archivos candidatos validados correctamente."

    def stage_specific_files(
        self,
        files: Sequence[str],
        *,
        baseline: Optional[GitTreeSnapshot] = None,
    ) -> tuple[bool, str]:
        """
        Prepara explícitamente los archivos indicados mediante `git add -- <archivos>`,
        previa validación de seguridad y protección de cambios preexistentes.
        """
        ok, reason = self.validate_commit_candidates(files, baseline=baseline)
        if not ok:
            return False, reason

        norm_files = [_normalize_rel_path(f) for f in files if _normalize_rel_path(f)]
        proc = self._run_git(["add", "--", *norm_files])
        if proc.returncode != 0:
            err_msg = redact_secrets((proc.stderr or proc.stdout or "").strip())
            return False, f"Error al ejecutar git add en archivos específicos: {err_msg}"

        return True, f"Archivos preparados correctamente: {', '.join(norm_files)}"

    def commit_staged_changes(
        self,
        message: str,
        *,
        user_authorized: bool = False,
        tests_passed: bool = False,
        baseline: Optional[GitTreeSnapshot] = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Crea un commit únicamente si:
        - El usuario autorizó explícitamente la operación (`user_authorized=True`).
        - Las pruebas esenciales se ejecutaron y pasaron (`tests_passed=True`).
        - `git diff --cached` contiene cambios válidos, sin archivos sensibles ni secretos.
        - Ningún archivo preparado pertenece a cambios preexistentes en `baseline`.
        """
        if not user_authorized:
            return (
                False,
                "Commit detenido: se requiere autorización explícita del usuario para confirmar cambios en Git.",
                None,
            )
        if not tests_passed:
            return (
                False,
                "Commit detenido: las pruebas pertinentes deben ejecutarse y pasar antes de confirmar cambios.",
                None,
            )

        commit_msg = (message or "").strip()
        if not commit_msg:
            return False, "El mensaje de commit no puede estar vacío.", None

        snapshot = self.inspect_state()
        if not snapshot.staged_files:
            return False, "No hay cambios preparados en el área de staging (`git diff --cached` vacío).", None

        for staged_file in snapshot.staged_files:
            if is_sensitive_path(staged_file):
                return (
                    False,
                    f"Commit bloqueado: el archivo preparado '{staged_file}' es sensible.",
                    None,
                )

        if baseline is not None:
            overlapping = sorted(set(snapshot.staged_files) & baseline.all_dirty_files)
            if overlapping:
                return (
                    False,
                    "Commit bloqueado: el área de staging incluye archivos con cambios preexistentes "
                    f"({', '.join(overlapping)}).",
                    None,
                )

        cached_diff_proc = self._run_git(["diff", "--cached"])
        secret_hits = scan_text_for_secrets(cached_diff_proc.stdout or "")
        if secret_hits:
            return (
                False,
                "Commit bloqueado: se detectaron posibles secretos en `git diff --cached` "
                f"({', '.join(secret_hits)}).",
                None,
            )

        commit_proc = self._run_git(["commit", "-m", commit_msg])
        if commit_proc.returncode != 0:
            err = redact_secrets((commit_proc.stderr or commit_proc.stdout or "").strip())
            return False, f"No se pudo crear el commit: {err}", None

        hash_proc = self._run_git(["rev-parse", "--short", "HEAD"])
        commit_hash = hash_proc.stdout.strip() if hash_proc.returncode == 0 else None
        return True, f"Commit creado exitosamente ({commit_hash or 'HEAD'}).", commit_hash

    def push_to_origin_main(
        self,
        *,
        user_authorized: bool = False,
        expected_remote_substring: str = "J.A.R.V.I.S",
    ) -> tuple[bool, str]:
        """
        Publica la rama `main` en `origin` únicamente cuando se cumplen todas las condiciones
        de seguridad y autorización. Jamás utiliza `--force`.
        """
        if not user_authorized:
            return (
                False,
                "Push detenido: se requiere autorización explícita del usuario para publicar en origin/main.",
            )

        snapshot = self.inspect_state()
        if snapshot.branch != "main":
            return (
                False,
                f"Push detenido: la rama actual es '{snapshot.branch}' y no 'main'.",
            )

        origin_url = snapshot.remotes.get("origin", "")
        if not origin_url:
            return False, "Push detenido: el remoto 'origin' no está configurado."

        if expected_remote_substring and expected_remote_substring.lower() not in origin_url.lower():
            return (
                False,
                f"Push detenido: la URL de 'origin' ('{origin_url}') no coincide con el repositorio esperado.",
            )

        tracked_sensitive = self.find_tracked_sensitive_files(snapshot)
        if tracked_sensitive:
            return (
                False,
                "Push bloqueado por seguridad: existen archivos sensibles registrados en Git "
                f"({', '.join(tracked_sensitive)}).",
            )

        if snapshot.all_dirty_files:
            return (
                False,
                "Push detenido: el árbol de trabajo contiene cambios locales sin confirmar "
                f"({', '.join(sorted(snapshot.all_dirty_files))}).",
            )

        push_proc = self._run_git(["push", "origin", "main"])
        if push_proc.returncode == 0:
            return True, "Push a origin/main completado correctamente."

        err = redact_secrets((push_proc.stderr or push_proc.stdout or "").strip())
        return (
            False,
            f"El push a origin/main fue rechazado sin aplicar '--force': {err}",
        )
