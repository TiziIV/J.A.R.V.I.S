"""
Habilidad de Exportación Documental y Modo Estudio para J.A.R.V.I.S.
===================================================================
Coordina la conversión, estructuración y renderizado de documentos
en formatos Markdown (.md) y Microsoft Word (.docx).

Nombre oficial de la skill: 'document_export'

Responsabilidades del orquestador:
1. Validación estricta de parámetros de entrada (content, format, title, output_path, mode, overwrite).
2. Aplicación de plantillas pedagógicas cuando el modo es 'study'.
3. Transformación mediante el parser agnóstico hacia el Modelo Documental Intermedio (IR).
4. Renderizado a Markdown y/o Word (.docx) mediante renderizadores independientes.
5. Validación y resolución ultra-segura de rutas en Windows (anti-traversal, anti-UNC, anti-symlink/junction, nombres reservados).
6. Escritura atómica libre de colisiones concurrentes y limpieza segura de temporales.
7. Emisión de confirmación ejecutiva optimizada para lectura TTS y consola.
"""

import os
import ntpath
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from config.settings import BASE_DIR, FORBIDDEN_SYSTEM_PREFIXES
from skills.base_skill import BaseSkill
from skills.document_models import Document
from skills.document_parser import DocumentParser
from skills.document_templates import get_template, is_template_available
from skills.markdown_renderer import MarkdownRenderer
from skills.word_renderer import WordRenderer

# Directorio predeterminado de exportación dentro del proyecto
DEFAULT_EXPORT_DIR: Path = BASE_DIR / "exports"

# Nombres de dispositivos reservados en el sistema de archivos de Windows
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

# Caracteres no permitidos en nombres de archivo de Windows
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FILE_ATTRIBUTE_REPARSE_POINT: int = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def sanitize_filename(name: str) -> str:
    """
    Normaliza y sanea un nombre base de archivo para compatibilidad en Windows.
    Reemplaza caracteres no permitidos por guiones bajos y descarta nombres reservados.
    """
    clean = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if not clean:
        clean = "documento_exportado"

    # Windows ignora espacios y puntos finales antes de evaluar el nombre de dispositivo.
    stem = clean.split(".", 1)[0].strip(" .").upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"El nombre '{name}' coincide con un nombre reservado del sistema operativo Windows ({stem}).")

    return clean


class DocumentExportSkill(BaseSkill):
    """
    Skill oficial de exportación documental en Markdown y Word DOCX para J.A.R.V.I.S.
    """

    def __init__(self, allowed_roots: Sequence[Path | str] | None = None) -> None:
        """
        Inicializa la skill registrando sus metadatos descriptivos y raíces autorizadas.

        Args:
            allowed_roots (Sequence[Path | str], optional): Raíces de disco autorizadas.
                Si no se especifican, se utiliza exclusivamente `<BASE_DIR>/exports`
                y las raíces adicionales declaradas explícitamente en variables de entorno.
        """
        super().__init__(
            name="document_export",
            description=(
                "Exporta apuntes, guías, resúmenes y documentos técnicos a formatos Markdown (.md) "
                "y Microsoft Word (.docx). Permite activar plantillas estructuradas del Modo Estudio."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Contenido del documento en formato Markdown o texto estructurado."
                    },
                    "format": {
                        "type": "string",
                        "enum": ["md", "docx", "both"],
                        "description": "Formato de exportación: 'md', 'docx' o 'both'."
                    },
                    "title": {
                        "type": "string",
                        "description": "Título descriptivo del documento."
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Ruta o nombre de archivo de destino (opcional, debe residir en una raíz autorizada)."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["general", "study"],
                        "description": "Modo de procesamiento: 'general' o 'study' (Modo Estudio)."
                    },
                    "template": {
                        "type": "string",
                        "description": "Plantilla específica del Modo Estudio (ej. 'structured_notes', 'executive_summary')."
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Si se permite sobrescribir un archivo existente con el mismo nombre."
                    }
                },
                "required": ["content"]
            }
        )

        # Construcción de raíces de exportación autorizadas
        if allowed_roots is not None:
            self._allowed_roots = tuple(Path(os.path.abspath(Path(r))) for r in allowed_roots)
        else:
            self._allowed_roots = self._load_configured_roots()
        self._allowed_root_realpaths: tuple[Path, ...] = tuple(
            root.resolve(strict=False) for root in self._allowed_roots
        )

        # Componentes desacoplados (el renderizador Word se inicializa sin importar docx)
        self.parser = DocumentParser()
        self.md_renderer = MarkdownRenderer()
        self.word_renderer = WordRenderer()

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        """Retorna las raíces autorizadas actualmente configuradas."""
        return self._allowed_roots

    def _load_configured_roots(self) -> tuple[Path, ...]:
        """Carga las raíces autorizadas desde la configuración del entorno."""
        roots: list[Path] = [Path(os.path.abspath(DEFAULT_EXPORT_DIR))]

        extra_env = os.getenv("JARVIS_EXPORT_ROOTS", "").strip()
        if extra_env:
            for item in extra_env.split(os.pathsep):
                clean_item = item.strip()
                if clean_item:
                    try:
                        exp = Path(os.path.abspath(os.path.expandvars(clean_item)))
                        norm_lower = str(exp.resolve(strict=False)).lower().rstrip("\\/")
                        if any(norm_lower == f or norm_lower.startswith(f + "\\") for f in FORBIDDEN_SYSTEM_PREFIXES):
                            continue
                        if exp.exists() and exp.is_dir() and exp not in roots:
                            roots.append(exp)
                    except Exception:
                        continue

        return tuple(roots)

    def execute(self, params: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """
        Punto de entrada de ejecución de la skill.
        Acepta tanto un diccionario 'params' como argumentos clave (**kwargs).
        """
        args: dict[str, Any] = {}
        if isinstance(params, dict):
            args.update(params)
        args.update(kwargs)

        # 1. Validación de parámetros de entrada
        content_raw = args.get("content")
        if not content_raw or not isinstance(content_raw, str) or not content_raw.strip():
            return "Lo siento señor, no se puede exportar un documento sin contenido."

        content = content_raw.strip()
        raw_format = str(args.get("format", "md")).strip().lower()
        if raw_format not in ("md", "docx", "both"):
            return f"Lo siento señor, el formato '{raw_format}' no es válido. Los formatos soportados son 'md', 'docx' y 'both'."

        raw_mode_value = args.get("mode", "general")
        if not isinstance(raw_mode_value, str) or raw_mode_value.strip().lower() not in ("general", "study"):
            return "Lo siento señor, el modo debe ser 'general' o 'study'."
        raw_mode = raw_mode_value.strip().lower()
        title = args.get("title")
        if title:
            title = str(title).strip()
        template_name = args.get("template")
        if template_name is not None:
            if not isinstance(template_name, str) or not is_template_available(template_name):
                return f"Lo siento señor, la plantilla '{template_name}' no es válida."
            template_name = template_name.strip()
        overwrite_value = args.get("overwrite", False)
        if not isinstance(overwrite_value, bool):
            return "Lo siento señor, 'overwrite' debe ser un valor booleano true o false."
        overwrite = overwrite_value
        output_path = args.get("output_path")

        # 2. Aplicación de plantilla pedagógica en Modo Estudio
        processed_content = content
        if raw_mode == "study" or template_name:
            template = get_template(template_name)
            processed_content = template.format_document(
                content=content,
                title=title or "Apuntes de Estudio",
                metadata={"mode": "study", "template": template.template_id}
            )

        # 3. Transformación al Modelo Documental Intermedio (IR)
        try:
            document: Document = self.parser.parse(
                content=processed_content,
                title=title,
                metadata={"mode": raw_mode}
            )
        except Exception as exc:
            return f"Lo siento señor, ocurrió un error al analizar la estructura del documento: {exc}"

        # 4. Orquestación del renderizado y escritura segura
        try:
            results: dict[str, Path] = {}
            errors: dict[str, str] = {}

            reserved_pair: dict[str, tuple[Path, tuple[int, int, int]]] = {}
            both_paths: dict[str, Path] = {}
            if raw_format == "both":
                try:
                    base_path = self._resolve_safe_destination(
                        document.title, ".md", output_path, overwrite=True, normalize_both=True
                    )
                    if overwrite:
                        both_paths = {"md": base_path, "docx": base_path.with_suffix(".docx")}
                    else:
                        reserved_pair = self._reserve_unique_format_pair(base_path)
                        both_paths = {key: value[0] for key, value in reserved_pair.items()}
                except Exception as exc:
                    return self._format_response(document.title, {}, {"md": str(exc), "docx": str(exc)}, raw_format)

            if raw_format in ("md", "both"):
                try:
                    md_output = str(both_paths["md"]) if raw_format == "both" else output_path
                    md_path = self._export_markdown(
                        document, md_output, overwrite or bool(reserved_pair),
                        reserved_identity=reserved_pair.get("md", (None, None))[1],
                    )
                    results["md"] = md_path
                except Exception as exc:
                    errors["md"] = str(exc)
                    if "md" in reserved_pair:
                        self._remove_owned_reservation(*reserved_pair["md"])

            if raw_format in ("docx", "both"):
                try:
                    docx_output = str(both_paths["docx"]) if raw_format == "both" else output_path
                    docx_path = self._export_word(
                        document, docx_output, overwrite or bool(reserved_pair),
                        reserved_identity=reserved_pair.get("docx", (None, None))[1],
                    )
                    results["docx"] = docx_path
                except Exception as exc:
                    errors["docx"] = str(exc)
                    if "docx" in reserved_pair:
                        self._remove_owned_reservation(*reserved_pair["docx"])

            # 5. Generación de confirmación ejecutiva según resultados
            return self._format_response(document.title, results, errors, raw_format)

        except Exception as exc:
            return f"Lo siento señor, ocurrió un fallo imprevisto durante la exportación documental: {exc}"

    def _export_markdown(
        self, document: Document, output_path: Any, overwrite: bool,
        reserved_identity: tuple[int, int, int] | None = None,
    ) -> Path:
        """Renderiza y escribe atómicamente el documento en formato Markdown."""
        # El renderizado en memoria evita dejar una reserva vacía si el renderer falla.
        md_text = self.md_renderer.render(document)
        dest_file = self._resolve_safe_destination(
            document.title,
            ext=".md",
            output_path=output_path,
            overwrite=overwrite
        )
        reservation_identity = reserved_identity or (None if overwrite else self._file_identity(dest_file))
        try:
            self._write_file_atomically(
                dest_file, md_text.encode("utf-8"), overwrite=overwrite,
                reserved_identity=reservation_identity,
            )
            return dest_file
        except Exception:
            if reservation_identity is not None:
                self._remove_owned_reservation(dest_file, reservation_identity)
            raise

    def _export_word(
        self, document: Document, output_path: Any, overwrite: bool,
        reserved_identity: tuple[int, int, int] | None = None,
    ) -> Path:
        """Renderiza y escribe atómicamente el documento en formato Word DOCX."""
        dest_file = self._resolve_safe_destination(
            document.title,
            ext=".docx",
            output_path=output_path,
            overwrite=overwrite
        )

        # Usar un archivo temporal en el mismo directorio de destino para garantizar atomicidad
        reservation_identity = reserved_identity or (None if overwrite else self._file_identity(dest_file))
        target_dir = dest_file.parent
        self._assert_safe_destination(dest_file)
        target_dir.mkdir(parents=True, exist_ok=True)
        self._assert_safe_destination(dest_file)
        temp_file = target_dir / f".~{dest_file.name}.{uuid.uuid4().hex}.tmp"

        try:
            temp_fd = os.open(str(temp_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            temp_stat = os.fstat(temp_fd)
            temp_identity = (temp_stat.st_dev, temp_stat.st_ino, temp_stat.st_size)
            os.close(temp_fd)
            self._assert_safe_destination(temp_file)
            if self._file_identity(temp_file) != temp_identity:
                raise PermissionError("El temporal Word cambió antes de iniciar el renderizado.")
            self.word_renderer.render_to_file(document, temp_file)
            self._assert_safe_destination(temp_file)
            if self._file_identity(temp_file)[:2] != temp_identity[:2]:
                raise PermissionError("El temporal Word cambió durante el renderizado.")
            with temp_file.open("r+b") as rendered_file:
                os.fsync(rendered_file.fileno())
            self._replace_with_retry(temp_file, dest_file, reservation_identity)
            return dest_file
        except Exception:
            if "temp_identity" in locals():
                self._remove_owned_file(temp_file, temp_identity)
            if reservation_identity is not None:
                self._remove_owned_reservation(dest_file, reservation_identity)
            raise

    def _resolve_safe_destination(
        self,
        doc_title: str,
        ext: str,
        output_path: Any,
        overwrite: bool,
        *,
        normalize_both: bool = False,
    ) -> Path:
        """
        Valida rigurosamente la seguridad de la ruta en Windows y previene colisiones concurrentes.
        """
        safe_title = sanitize_filename(doc_title)
        default_name = f"{safe_title}{ext}"

        if not output_path:
            target_dir = self._allowed_roots[0]
            candidate = target_dir / default_name
        else:
            raw_path_str = str(output_path).strip()

            # Detección inmediata de path traversal crudo
            if ".." in Path(raw_path_str).parts:
                raise PermissionError("Acceso denegado: Intento de path traversal ('..') detectado en la ruta.")

            # Bloqueo estricto de rutas UNC
            if raw_path_str.startswith(r"\\") or raw_path_str.startswith("//"):
                raise PermissionError("Acceso denegado: Las rutas de red UNC no están autorizadas.")

            drive, _ = ntpath.splitdrive(raw_path_str)
            if drive and not raw_path_str.startswith(drive + "\\") and not raw_path_str.startswith(drive + "/"):
                raise PermissionError("Acceso denegado: Las rutas relativas a una unidad (por ejemplo C:archivo) no están autorizadas.")
            if raw_path_str.startswith(("\\", "/")) and not (raw_path_str.startswith("\\\\") or raw_path_str.startswith("//")) and not drive:
                raise PermissionError("Acceso denegado: Las rutas con raíz sin unidad no están autorizadas.")
            candidate_path = Path(raw_path_str)

            # Si es un nombre de archivo simple o ruta relativa
            if not candidate_path.is_absolute():
                relative_is_directory = raw_path_str.endswith(("\\", "/"))
                directory_parts = candidate_path.parts if relative_is_directory else candidate_path.parts[:-1]
                target_dir = self._allowed_roots[0].joinpath(*directory_parts)
                for part in directory_parts:
                    self._validate_windows_component(part)
                clean_name = (
                    default_name
                    if relative_is_directory
                    else self._validated_explicit_name(candidate_path.name, ext, normalize_both)
                )
                candidate = target_dir / clean_name
            else:
                # Es ruta absoluta: validar pertenencia física
                if candidate_path.is_dir() or raw_path_str.endswith(("\\", "/")):
                    target_dir = candidate_path
                    clean_name = default_name
                else:
                    target_dir = candidate_path.parent
                    clean_name = self._validated_explicit_name(candidate_path.name, ext, normalize_both)

                # Comprobación de prefijos de sistema prohibidos
                candidate = target_dir / clean_name
                self._assert_safe_destination(candidate)
                resolved_target_dir = target_dir.resolve(strict=False)
                norm_lower = str(resolved_target_dir).lower().rstrip("\\/")
                for forbidden in FORBIDDEN_SYSTEM_PREFIXES:
                    if norm_lower == forbidden or norm_lower.startswith(forbidden + "\\"):
                        raise PermissionError(f"Acceso denegado: La ubicación '{target_dir}' es un directorio protegido de Windows.")

                # Comprobación estricta de pertenencia a una raíz autorizada
                self._matching_root(candidate)

        # Manejo de concurrencia y prevención de colisiones cuando overwrite=False
        self._assert_safe_destination(candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_destination(candidate)
        if overwrite:
            return candidate

        return self._find_and_reserve_unique_sequential_file(candidate)

    def _find_and_reserve_unique_sequential_file(self, base_file: Path) -> Path:
        """
        Encuentra y reserva atómicamente el siguiente nombre secuencial disponible
        utilizando os.O_CREAT | os.O_EXCL para evitar colisiones entre procesos o hilos concurrentes:
        ej. doc.docx -> doc_(1).docx -> doc_(2).docx
        """
        target_dir = base_file.parent
        self._assert_safe_destination(base_file)
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = base_file.stem
        ext = base_file.suffix

        # Limpiar si ya contenía un sufijo tipo _(N)
        match = re.match(r"^(.*?)(?:_\((\d+)\))?$", stem)
        clean_stem = match.group(1) if match else stem

        candidate = base_file
        idx = 1
        while True:
            try:
                # Reserva atómica libre de condiciones TOCTOU
                fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                info = os.fstat(fd)
                os.close(fd)
                identity = (info.st_dev, info.st_ino, info.st_size)
                try:
                    self._assert_safe_destination(candidate)
                except Exception:
                    self._remove_owned_reservation(candidate, identity)
                    raise
                return candidate
            except FileExistsError:
                candidate = target_dir / f"{clean_stem}_({idx}){ext}"
                idx += 1

    def _write_file_atomically(
        self, target_file: Path, data: bytes, overwrite: bool,
        reserved_identity: tuple[int, int, int] | None = None,
    ) -> None:
        """
        Escribe datos binarios de forma atómica en Windows mediante archivo temporal y reemplazo seguro.
        """
        self._assert_safe_destination(target_file)
        if reserved_identity is not None and self._file_identity(target_file) != reserved_identity:
            raise PermissionError("La reserva del destino cambió antes de escribir.")
        target_dir = target_file.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        temp_file = target_dir / f".~{target_file.name}.{uuid.uuid4().hex[:8]}.tmp"
        temp_identity: tuple[int, int, int] | None = None
        try:
            with temp_file.open("xb") as stream:
                temp_stat = os.fstat(stream.fileno())
                temp_identity = (temp_stat.st_dev, temp_stat.st_ino, temp_stat.st_size)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._replace_with_retry(temp_file, target_file, reserved_identity)
        except Exception:
            if temp_identity is not None:
                self._remove_owned_file(temp_file, temp_identity)
            raise

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int]:
        """Identifica un archivo sin seguir enlaces para limpiar solo reservas propias."""
        info = os.lstat(path)
        return info.st_dev, info.st_ino, info.st_size

    def _remove_owned_reservation(self, path: Path, identity: tuple[int, int, int]) -> None:
        """Elimina una reserva vacía solo si sigue siendo el mismo archivo creado aquí."""
        try:
            info = os.lstat(path)
            current = (info.st_dev, info.st_ino, info.st_size)
            if current == identity and stat.S_ISREG(info.st_mode) and info.st_size == 0:
                path.unlink()
        except OSError:
            pass

    def _remove_owned_file(self, path: Path, identity: tuple[int, int, int]) -> None:
        """Limpia un temporal parcial si conserva la identidad del archivo creado por esta operación."""
        try:
            info = os.lstat(path)
            if (info.st_dev, info.st_ino) == identity[:2] and stat.S_ISREG(info.st_mode):
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _path_is_reparse_point(path: Path) -> bool:
        """Detecta symlinks y reparse points sin seguir el destino del enlace."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PermissionError(f"No se pudo inspeccionar el componente '{path.name}' de forma segura.") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _matching_root(self, candidate: Path) -> tuple[Path, Path]:
        absolute_candidate = Path(os.path.abspath(candidate))
        for root, expected_real_root in zip(self._allowed_roots, self._allowed_root_realpaths):
            if self._is_within(absolute_candidate, root):
                return root, expected_real_root
        raise PermissionError("Acceso denegado: el destino no reside bajo una raíz de exportación autorizada.")

    def _assert_safe_destination(self, candidate: Path) -> None:
        """Revalida físicamente raíz, ancestros y destino justo antes de operar."""
        root, expected_real_root = self._matching_root(candidate)
        relative_parts = Path(os.path.abspath(candidate)).relative_to(root).parts
        for part in relative_parts:
            self._validate_windows_component(part)

        chain: list[Path] = []
        current = Path(os.path.abspath(candidate))
        while current.parent != current:
            chain.append(current)
            current = current.parent
        for component in reversed(chain):
            if os.path.lexists(component) and self._path_is_reparse_point(component):
                raise PermissionError(
                    f"Acceso denegado: se detectó symlink, junction o reparse point en '{component}'."
                )

        try:
            current_real_root = root.resolve(strict=False)
            physical_parent = candidate.parent.resolve(strict=False)
        except OSError as exc:
            raise PermissionError("No se pudo resolver de forma segura la ruta física de exportación.") from exc
        if current_real_root != expected_real_root:
            raise PermissionError("Acceso denegado: la raíz autorizada cambió físicamente desde que se inicializó la skill.")
        if not self._is_within(physical_parent, expected_real_root):
            raise PermissionError("Acceso denegado: la ruta física del destino sale de la raíz autorizada.")
        norm_lower = str(physical_parent).lower().rstrip("\\/")
        for forbidden in FORBIDDEN_SYSTEM_PREFIXES:
            if norm_lower == forbidden or norm_lower.startswith(forbidden + "\\"):
                raise PermissionError("Acceso denegado: la ubicación pertenece a un directorio protegido de Windows.")

        if os.path.lexists(candidate):
            if self._path_is_reparse_point(candidate):
                raise PermissionError("Acceso denegado: el archivo de destino existente es un enlace o reparse point.")
            try:
                physical_target = candidate.resolve(strict=True)
            except OSError as exc:
                raise PermissionError("No se pudo validar físicamente el destino existente.") from exc
            if not self._is_within(physical_target, expected_real_root):
                raise PermissionError("Acceso denegado: el destino físico sale de la raíz autorizada.")

    def _validated_explicit_name(self, name: str, ext: str, normalize_both: bool) -> str:
        base = name
        if normalize_both and Path(base).suffix.lower() in (".md", ".docx"):
            base = Path(base).stem
        if Path(base).suffix.lower() == ext.lower():
            base = Path(base).stem
        self._validate_windows_component(base)
        return f"{base}{ext}"

    @staticmethod
    def _validate_windows_component(component: str) -> None:
        if not component or component in (".", "..") or INVALID_FILENAME_CHARS.search(component):
            raise ValueError("La ruta contiene un componente vacío o con caracteres no válidos.")
        if component.rstrip(" .") != component:
            raise ValueError(f"El componente '{component}' termina en espacios o puntos, lo que Windows normaliza.")
        windows_stem = component.split(".", 1)[0].strip(" .").upper()
        if windows_stem in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"El componente '{component}' es un nombre reservado de Windows.")

    def _reserve_unique_format_pair(
        self, base_file: Path,
    ) -> dict[str, tuple[Path, tuple[int, int, int]]]:
        """Reserva los dos formatos con una base común y revierte solo su reserva parcial."""
        parent = base_file.parent
        stem = base_file.stem
        self._assert_safe_destination(base_file)
        for index in range(1000000):
            base = stem if index == 0 else f"{stem}_({index})"
            candidates = {"md": parent / f"{base}.md", "docx": parent / f"{base}.docx"}
            reservations: dict[str, tuple[Path, tuple[int, int, int]]] = {}
            collision = False
            for kind, candidate in candidates.items():
                self._assert_safe_destination(candidate)
                try:
                    fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    info = os.fstat(fd)
                    os.close(fd)
                    identity = (info.st_dev, info.st_ino, info.st_size)
                    reservations[kind] = (candidate, identity)
                    self._assert_safe_destination(candidate)
                except FileExistsError:
                    collision = True
                    break
                except Exception:
                    for reserved_path, reserved_identity in reservations.values():
                        self._remove_owned_reservation(reserved_path, reserved_identity)
                    raise
            if not collision:
                return reservations
            for reserved_path, identity in reservations.values():
                self._remove_owned_reservation(reserved_path, identity)
        raise FileExistsError("No se encontró un nombre común libre para los formatos Markdown y Word.")

    def _replace_with_retry(
        self,
        source: Path,
        destination: Path,
        reserved_identity: tuple[int, int, int] | None,
    ) -> None:
        """Reintenta brevemente solo bloqueos transitorios de reemplazo en Windows."""
        for attempt in range(4):
            self._assert_safe_destination(source)
            self._assert_safe_destination(destination)
            if reserved_identity is not None and self._file_identity(destination) != reserved_identity:
                raise PermissionError("La reserva del destino cambió antes del reemplazo atómico.")
            try:
                os.replace(source, destination)
                return
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32) or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _format_response(
        self,
        title: str,
        results: dict[str, Path],
        errors: dict[str, str],
        requested_format: str,
    ) -> str:
        """Genera el mensaje ejecutivo final en lenguaje natural para TTS y consola."""
        if requested_format == "both":
            if "md" in results and "docx" in results:
                return (
                    f"Señor, he exportado el documento '{title}' exitosamente en ambos formatos: "
                    f"Word en '{results['docx']}' y Markdown en '{results['md']}'."
                )
            elif "md" in results and "docx" not in results:
                err = errors.get("docx", "Error desconocido")
                return (
                    f"Señor, se generó el archivo Markdown en '{results['md']}', "
                    f"pero la exportación a Word falló debido a: {err}."
                )
            elif "docx" in results and "md" not in results:
                err = errors.get("md", "Error desconocido")
                return (
                    f"Señor, se generó el archivo Word en '{results['docx']}', "
                    f"pero la exportación a Markdown falló debido a: {err}."
                )
            else:
                return f"Lo siento señor, falló la exportación en ambos formatos: {errors}."

        elif requested_format == "docx":
            if "docx" in results:
                return f"Señor, he exportado sus apuntes de '{title}' en formato Word en '{results['docx']}'."
            else:
                return f"Lo siento señor, no se pudo generar el documento Word: {errors.get('docx', 'Error desconocido')}."

        else:  # md
            if "md" in results:
                return f"Señor, he exportado sus apuntes de '{title}' en formato Markdown en '{results['md']}'."
            else:
                return f"Lo siento señor, no se pudo generar el documento Markdown: {errors.get('md', 'Error desconocido')}."
