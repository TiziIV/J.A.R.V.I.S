"""Proveedor contextual de sistema de archivos en solo lectura dentro de raíces autorizadas (N7).

Responsabilidades:
1. Consultar existencia y metadatos básicos (`size_bytes`, `extension`, `file_category`,
   `modified_at`) de rutas previamente validadas por `ContextAccessPolicy`.
2. Explorar estructuras de directorios con límites estrictos de profundidad (`max_depth`)
   y cantidad máxima de entradas (`max_entries`).
3. Localizar archivos relevantes para un objetivo (p. ej. archivos `.ini`, `.cfg`, `.json`)
   dentro de una ubicación autorizada sin leer contenido privado ni recorrer todo el disco.

Garantías de seguridad:
- Estrictamente de solo lectura: no contiene llamadas a `open(..., 'w')`, `write_text`,
  `write_bytes`, `unlink`, `remove`, `rmdir`, `rmtree`, `replace` ni `rename`.
- No sigue enlaces simbólicos ni junctions NTFS (`FILE_ATTRIBUTE_REPARSE_POINT`).
- Excluye y rechaza archivos sensibles (`.env`, `prompts_history.txt`, `memory_store.json`,
  claves privadas y tokens).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from typing import Optional, Sequence

from src.context.models import (
    AvailabilityStatus,
    DirectorySnapshot,
    FileMetadataItem,
    FileSystemContextInfo,
    utc_now_iso,
)
from src.context.policy import (
    ContextAccessPolicy,
    is_sensitive_context_filename,
    sanitize_untrusted_label,
)


_CONFIG_EXTENSIONS: frozenset[str] = frozenset(
    {".ini", ".cfg", ".conf", ".json", ".yaml", ".yml", ".toml", ".xml"}
)
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".ts", ".ps1", ".bat", ".sh", ".c", ".cpp", ".h", ".rs", ".go"}
)
_DOC_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".rst", ".docx", ".pdf", ".html"}
)
_DATA_EXTENSIONS: frozenset[str] = frozenset(
    {".csv", ".tsv", ".jsonl", ".sqlite", ".db"}
)

_IGNORED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        "build",
        "dist",
    }
)


def classify_file_category(extension: str, *, is_dir: bool = False) -> str:
    """Clasifica el tipo de entrada según su extensión normalizada."""
    if is_dir:
        return "directory"
    ext_low = (extension or "").lower()
    if ext_low in _CONFIG_EXTENSIONS:
        return "config"
    if ext_low in _CODE_EXTENSIONS:
        return "code"
    if ext_low in _DOC_EXTENSIONS:
        return "document"
    if ext_low in _DATA_EXTENSIONS:
        return "data"
    return "other"


class FileSystemContextProvider:
    """Observador de archivos y directorios acotado a `AUTHORIZED_FILE_ROOTS`."""

    def __init__(self, policy: Optional[ContextAccessPolicy] = None) -> None:
        self.policy: ContextAccessPolicy = policy or ContextAccessPolicy()

    def inspect_path(self, raw_path: str | Path) -> FileMetadataItem:
        """Consulta existencia y metadatos básicos de una ruta dentro de las raíces autorizadas.

        Lanza `ContextAccessDeniedError` si la ruta está fuera de las raíces permitidas,
        contiene traversal `..`, apunta a un archivo sensible o atraviesa un symlink/junction.
        """
        self.policy.assert_read_only_operation("inspect_path")
        resolved, auth_root = self.policy.validate_read_path(raw_path, must_exist=False)

        exists = resolved.exists()
        if not exists:
            ext = resolved.suffix.lower()
            return FileMetadataItem(
                path=str(resolved),
                name=sanitize_untrusted_label(resolved.name, max_len=120),
                exists=False,
                is_dir=False,
                is_file=False,
                extension=ext,
                file_category=classify_file_category(ext, is_dir=False),
                size_bytes=None,
                modified_at=None,
                authorized_root=str(auth_root),
            )

        st = os.lstat(resolved)
        is_dir = stat.S_ISDIR(st.st_mode)
        is_file = stat.S_ISREG(st.st_mode)
        ext = "" if is_dir else resolved.suffix.lower()
        size_val = int(st.st_size) if is_file else None
        mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

        return FileMetadataItem(
            path=str(resolved),
            name=sanitize_untrusted_label(resolved.name, max_len=120),
            exists=True,
            is_dir=is_dir,
            is_file=is_file,
            extension=ext,
            file_category=classify_file_category(ext, is_dir=is_dir),
            size_bytes=size_val,
            modified_at=mtime_iso,
            authorized_root=str(auth_root),
        )

    def list_directory(
        self,
        raw_dir_path: str | Path,
        *,
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = None,
    ) -> DirectorySnapshot:
        """Recorre un directorio autorizado con límites explícitos de profundidad y cantidad."""
        self.policy.assert_read_only_operation("list_directory")
        resolved_dir, auth_root = self.policy.validate_read_path(raw_dir_path, must_exist=True)

        if not resolved_dir.is_dir():
            raise ValueError(f"La ruta solicitada no es un directorio: '{resolved_dir.name}'.")

        depth_limit = min(
            max(1, int(max_depth)) if max_depth is not None else self.policy.max_dir_depth,
            self.policy.max_dir_depth,
        )
        entry_limit = min(
            max(1, int(max_entries)) if max_entries is not None else self.policy.max_dir_entries,
            self.policy.max_dir_entries,
        )

        entries: list[FileMetadataItem] = []
        total_scanned = 0
        max_depth_seen = 0
        truncated = False

        queue: deque[tuple[Path, int]] = deque([(resolved_dir, 1)])
        while queue:
            current_dir, current_depth = queue.popleft()
            if current_depth > depth_limit:
                continue
            if self.policy.is_reparse_point_or_symlink(current_dir):
                continue

            try:
                dir_items = sorted(os.scandir(current_dir), key=lambda e: e.name.lower())
            except OSError:
                continue

            for entry in dir_items:
                total_scanned += 1
                entry_path = Path(entry.path)
                name_low = entry.name.lower()

                # Omitir archivos sensibles y enlaces simbólicos / junctions
                if is_sensitive_context_filename(entry.name):
                    continue
                if self.policy.is_reparse_point_or_symlink(entry_path):
                    continue

                if len(entries) >= entry_limit:
                    truncated = True
                    break

                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                is_dir = stat.S_ISDIR(st.st_mode)
                is_file = stat.S_ISREG(st.st_mode)
                if is_dir and name_low in _IGNORED_SCAN_DIRS:
                    continue

                max_depth_seen = max(max_depth_seen, current_depth)
                ext = "" if is_dir else entry_path.suffix.lower()
                mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

                entries.append(
                    FileMetadataItem(
                        path=str(entry_path),
                        name=sanitize_untrusted_label(entry.name, max_len=120),
                        exists=True,
                        is_dir=is_dir,
                        is_file=is_file,
                        extension=ext,
                        file_category=classify_file_category(ext, is_dir=is_dir),
                        size_bytes=int(st.st_size) if is_file else None,
                        modified_at=mtime_iso,
                        authorized_root=str(auth_root),
                    )
                )

                if is_dir and current_depth < depth_limit:
                    queue.append((entry_path, current_depth + 1))

            if truncated:
                break

        return DirectorySnapshot(
            root_path=str(resolved_dir),
            entries=tuple(entries),
            total_entries_scanned=total_scanned,
            max_depth_reached=max_depth_seen,
            truncated=truncated,
        )

    def find_relevant_files(
        self,
        search_root: str | Path,
        *,
        query: str = "",
        extensions: Optional[Sequence[str]] = None,
        max_results: int = 20,
    ) -> tuple[FileMetadataItem, ...]:
        """Busca archivos relevantes para un objetivo dentro de una ubicación autorizada."""
        self.policy.assert_read_only_operation("find_relevant_files")
        resolved_dir, auth_root = self.policy.validate_read_path(search_root, must_exist=True)
        if not resolved_dir.is_dir():
            raise ValueError(f"La ruta de búsqueda no es un directorio: '{resolved_dir.name}'.")

        tokens = [
            tok
            for tok in re.split(r"[^a-z0-9_.-]+", (query or "").lower())
            if len(tok) >= 2
        ]
        allowed_exts = (
            {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}
            if extensions
            else None
        )
        cap = max(1, min(int(max_results), self.policy.max_dir_entries))
        scan_budget = max(self.policy.max_dir_entries * 5, 100)
        depth_limit = self.policy.max_dir_depth

        matches: list[FileMetadataItem] = []
        scanned = 0
        queue: deque[tuple[Path, int]] = deque([(resolved_dir, 1)])

        while queue and len(matches) < cap and scanned < scan_budget:
            current_dir, current_depth = queue.popleft()
            if current_depth > depth_limit:
                continue
            if self.policy.is_reparse_point_or_symlink(current_dir):
                continue

            try:
                dir_items = sorted(os.scandir(current_dir), key=lambda e: e.name.lower())
            except OSError:
                continue

            for entry in dir_items:
                scanned += 1
                if scanned > scan_budget or len(matches) >= cap:
                    break
                entry_path = Path(entry.path)
                name_low = entry.name.lower()

                if is_sensitive_context_filename(entry.name):
                    continue
                if self.policy.is_reparse_point_or_symlink(entry_path):
                    continue

                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                is_dir = stat.S_ISDIR(st.st_mode)
                is_file = stat.S_ISREG(st.st_mode)
                if is_dir:
                    if name_low not in _IGNORED_SCAN_DIRS and current_depth < depth_limit:
                        queue.append((entry_path, current_depth + 1))
                    continue

                if not is_file:
                    continue

                ext = entry_path.suffix.lower()
                if allowed_exts is not None and ext not in allowed_exts:
                    continue
                if tokens:
                    haystack = f"{name_low} {str(entry_path).lower()}"
                    if not any(tok in haystack for tok in tokens):
                        continue

                mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                matches.append(
                    FileMetadataItem(
                        path=str(entry_path),
                        name=sanitize_untrusted_label(entry.name, max_len=120),
                        exists=True,
                        is_dir=False,
                        is_file=True,
                        extension=ext,
                        file_category=classify_file_category(ext, is_dir=False),
                        size_bytes=int(st.st_size),
                        modified_at=mtime_iso,
                        authorized_root=str(auth_root),
                    )
                )

        return tuple(matches)

    def collect(
        self,
        *,
        target_paths: Optional[Sequence[str | Path]] = None,
        search_root: Optional[str | Path] = None,
        search_query: str = "",
    ) -> FileSystemContextInfo:
        """Consolida la información contextual de archivos solicitados en raíces autorizadas."""
        self.policy.assert_read_only_operation("collect_filesystem_context")
        now_iso = utc_now_iso()

        queried_files: list[FileMetadataItem] = []
        directories: list[DirectorySnapshot] = []
        relevant_matches: list[FileMetadataItem] = []
        rejected_paths: list[str] = []

        for raw_p in target_paths or ():
            try:
                meta = self.inspect_path(raw_p)
                queried_files.append(meta)
                if meta.exists and meta.is_dir:
                    directories.append(self.list_directory(meta.path))
            except Exception:
                rejected_paths.append(sanitize_untrusted_label(str(raw_p), max_len=120))

        if search_root is not None:
            try:
                dir_snap = self.list_directory(search_root)
                directories.append(dir_snap)
                if search_query:
                    relevant_matches.extend(
                        self.find_relevant_files(search_root, query=search_query)
                    )
            except Exception:
                rejected_paths.append(sanitize_untrusted_label(str(search_root), max_len=120))

        status = AvailabilityStatus.AVAILABLE
        if rejected_paths and not (queried_files or directories or relevant_matches):
            status = AvailabilityStatus.RESTRICTED
        elif rejected_paths:
            status = AvailabilityStatus.PARTIAL

        return FileSystemContextInfo(
            authorized_roots=tuple(str(r) for r in self.policy.authorized_roots),
            queried_files=tuple(queried_files),
            directories=tuple(directories),
            relevant_matches=tuple(relevant_matches),
            rejected_paths=tuple(rejected_paths),
            captured_at=now_iso,
            status=status,
        )
