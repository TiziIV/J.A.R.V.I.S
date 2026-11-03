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
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Sequence

from config.settings import BASE_DIR, FORBIDDEN_SYSTEM_PREFIXES
from skills.base_skill import BaseSkill
from skills.document_models import Document
from skills.document_parser import DocumentParser
from skills.document_templates import get_template
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


def sanitize_filename(name: str) -> str:
    """
    Normaliza y sanea un nombre base de archivo para compatibilidad en Windows.
    Reemplaza caracteres no permitidos por guiones bajos y descarta nombres reservados.
    """
    clean = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    if not clean:
        clean = "documento_exportado"

    stem = clean.split(".")[0].upper()
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
            self._allowed_roots = tuple(Path(r).resolve() for r in allowed_roots)
        else:
            self._allowed_roots = self._load_configured_roots()

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
        roots: list[Path] = [DEFAULT_EXPORT_DIR.resolve()]

        extra_env = os.getenv("JARVIS_EXPORT_ROOTS", "").strip()
        if extra_env:
            for item in extra_env.split(os.pathsep):
                clean_item = item.strip()
                if clean_item:
                    try:
                        exp = Path(os.path.expandvars(clean_item)).resolve()
                        norm_lower = str(exp).lower().rstrip("\\/")
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

        raw_mode = str(args.get("mode", "general")).strip().lower()
        title = args.get("title")
        if title:
            title = str(title).strip()
        template_name = args.get("template")
        overwrite = bool(args.get("overwrite", False))
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

            if raw_format in ("md", "both"):
                try:
                    md_path = self._export_markdown(document, output_path, overwrite)
                    results["md"] = md_path
                except Exception as exc:
                    errors["md"] = str(exc)

            if raw_format in ("docx", "both"):
                try:
                    docx_path = self._export_word(document, output_path, overwrite)
                    results["docx"] = docx_path
                except Exception as exc:
                    errors["docx"] = str(exc)

            # 5. Generación de confirmación ejecutiva según resultados
            return self._format_response(document.title, results, errors, raw_format)

        except Exception as exc:
            return f"Lo siento señor, ocurrió un fallo imprevisto durante la exportación documental: {exc}"

    def _export_markdown(self, document: Document, output_path: Any, overwrite: bool) -> Path:
        """Renderiza y escribe atómicamente el documento en formato Markdown."""
        dest_file = self._resolve_safe_destination(
            document.title,
            ext=".md",
            output_path=output_path,
            overwrite=overwrite
        )
        md_text = self.md_renderer.render(document)
        try:
            self._write_file_atomically(dest_file, md_text.encode("utf-8"), overwrite=overwrite)
            return dest_file
        except Exception:
            if not overwrite and dest_file.exists() and dest_file.stat().st_size == 0:
                try:
                    dest_file.unlink()
                except OSError:
                    pass
            raise

    def _export_word(self, document: Document, output_path: Any, overwrite: bool) -> Path:
        """Renderiza y escribe atómicamente el documento en formato Word DOCX."""
        dest_file = self._resolve_safe_destination(
            document.title,
            ext=".docx",
            output_path=output_path,
            overwrite=overwrite
        )

        # Usar un archivo temporal en el mismo directorio de destino para garantizar atomicidad
        target_dir = dest_file.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_file = target_dir / f".~{dest_file.name}.{uuid.uuid4().hex[:8]}.tmp"

        try:
            self.word_renderer.render_to_file(document, temp_file)
            # Reemplazo atómico sobre el destino final
            os.replace(temp_file, dest_file)
            return dest_file
        except Exception:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
            if not overwrite and dest_file.exists() and dest_file.stat().st_size == 0:
                try:
                    dest_file.unlink()
                except OSError:
                    pass
            raise

    def _resolve_safe_destination(
        self,
        doc_title: str,
        ext: str,
        output_path: Any,
        overwrite: bool,
    ) -> Path:
        """
        Valida rigurosamente la seguridad de la ruta en Windows y previene colisiones concurrentes.
        """
        safe_title = sanitize_filename(doc_title)
        default_name = f"{safe_title}{ext}"

        if not output_path:
            target_dir = self._allowed_roots[0]
            target_dir.mkdir(parents=True, exist_ok=True)
            candidate = target_dir / default_name
        else:
            raw_path_str = str(output_path).strip()

            # Detección inmediata de path traversal crudo
            if ".." in Path(raw_path_str).parts:
                raise PermissionError("Acceso denegado: Intento de path traversal ('..') detectado en la ruta.")

            # Bloqueo estricto de rutas UNC
            if raw_path_str.startswith(r"\\") or raw_path_str.startswith("//"):
                raise PermissionError("Acceso denegado: Las rutas de red UNC no están autorizadas.")

            candidate_path = Path(raw_path_str)

            # Si es un nombre de archivo simple o ruta relativa
            if not candidate_path.is_absolute():
                target_dir = self._allowed_roots[0]
                target_dir.mkdir(parents=True, exist_ok=True)
                clean_name = sanitize_filename(candidate_path.name)
                if not clean_name.lower().endswith(ext.lower()):
                    clean_name = f"{clean_name}{ext}"
                candidate = target_dir / clean_name
            else:
                # Es ruta absoluta: validar pertenencia física
                if candidate_path.is_dir() or raw_path_str.endswith(("\\", "/")):
                    target_dir = candidate_path
                    clean_name = default_name
                else:
                    target_dir = candidate_path.parent
                    clean_name = sanitize_filename(candidate_path.name)
                    if not clean_name.lower().endswith(ext.lower()):
                        clean_name = f"{clean_name}{ext}"

                # Comprobación de prefijos de sistema prohibidos
                resolved_target_dir = target_dir.resolve()
                norm_lower = str(resolved_target_dir).lower().rstrip("\\/")
                for forbidden in FORBIDDEN_SYSTEM_PREFIXES:
                    if norm_lower == forbidden or norm_lower.startswith(forbidden + "\\"):
                        raise PermissionError(f"Acceso denegado: La ubicación '{target_dir}' es un directorio protegido de Windows.")

                # Comprobación estricta de pertenencia a una raíz autorizada
                is_authorized = False
                for root in self._allowed_roots:
                    root_resolved = root.resolve()
                    try:
                        # is_relative_to verifica si target_dir está dentro de root_resolved
                        if resolved_target_dir == root_resolved or resolved_target_dir.is_relative_to(root_resolved):
                            is_authorized = True
                            break
                    except AttributeError:
                        # Fallback de compatibilidad
                        if str(resolved_target_dir).lower().startswith(str(root_resolved).lower()):
                            is_authorized = True
                            break

                if not is_authorized:
                    raise PermissionError(
                        f"Acceso denegado: La ruta '{target_dir}' no reside en ninguna de las raíces de exportación autorizadas."
                    )

                target_dir.mkdir(parents=True, exist_ok=True)
                candidate = target_dir / clean_name

        # Manejo de concurrencia y prevención de colisiones cuando overwrite=False
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
                os.close(fd)
                return candidate
            except FileExistsError:
                candidate = target_dir / f"{clean_stem}_({idx}){ext}"
                idx += 1

    def _write_file_atomically(self, target_file: Path, data: bytes, overwrite: bool) -> None:
        """
        Escribe datos binarios de forma atómica en Windows mediante archivo temporal y reemplazo seguro.
        """
        target_dir = target_file.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        temp_file = target_dir / f".~{target_file.name}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            temp_file.write_bytes(data)
            # Reemplazo atómico en Windows
            os.replace(temp_file, target_file)
        except Exception:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
            raise

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
