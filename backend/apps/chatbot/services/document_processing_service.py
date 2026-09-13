"""
Document Processing Service

Handles file upload processing, text extraction, chunking, and embedding creation.
Designed to be called from Celery tasks for asynchronous processing.

Usage:
    from chatbot.services import DocumentProcessingService

    # Process uploaded document
    DocumentProcessingService.process_document(
        document_id=doc.id,
        user_id=user.id
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Dict, Any, Optional
from uuid import UUID
import mimetypes
import os

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    Docx2txtLoader,
)

if TYPE_CHECKING:
    # Deferred so this module can also run as a standalone script (see
    # `__main__` below) without needing Django app registry / settings.
    from django.core.files.uploadedfile import UploadedFile
    from ..models import UserDocument
    from accounts.models import CustomUser


class DocumentProcessingService:
    """Service for processing uploaded documents."""

    # Supported file types
    SUPPORTED_TYPES = {
        "application/pdf": "pdf",
        "text/plain": "txt",
        "text/markdown": "md",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "doc",
    }

    # Shared by chunk_text() and chunk_documents()
    DEFAULT_SEPARATORS = [
        "\n\n",  # Paragraphs
        "\n",  # Lines
        ". ",  # Sentences
        ", ",  # Clauses
        " ",  # Words
        "",  # Characters
    ]

    @staticmethod
    def create_document_record(
        user: CustomUser,
        uploaded_file: UploadedFile,
        chat_session_id: Optional[UUID] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UserDocument:
        """
        Create initial document record from uploaded file.

        Args:
            user: User uploading the file
            uploaded_file: Django UploadedFile object
            chat_session_id: Optional associated chat session
            metadata: Additional metadata

        Returns:
            Created UserDocument instance

        Example:
            doc = DocumentProcessingService.create_document_record(
                user=request.user,
                uploaded_file=request.FILES['document'],
                chat_session_id=session.id
            )

            # Then trigger async processing
            process_document_task.delay(doc.id, user.id)
        """
        from ..models import UserDocument

        # Validate file type
        file_type = uploaded_file.content_type
        if file_type not in DocumentProcessingService.SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported file type: {file_type}. "
                f"Supported: {', '.join(DocumentProcessingService.SUPPORTED_TYPES.values())}"
            )

        # Create document record
        doc = UserDocument.objects.create(
            user=user,
            chat_session_id=chat_session_id,
            file=uploaded_file,
            file_name=uploaded_file.name,
            file_size=uploaded_file.size,
            file_type=file_type,
            processing_status="pending",
            metadata=metadata or {},
        )

        return doc

    @staticmethod
    def load_document_pages(file_path: str, file_type: str) -> List[Document]:
        """
        Load a document as LangChain ``Document`` objects, preserving the
        per-page metadata (e.g. PDF page numbers) the loaders attach —
        metadata that ``load_document_text()`` below throws away by
        flattening everything to one string.

        Args:
            file_path: Path to the file
            file_type: MIME type of the file

        Returns:
            One Document per PDF page, or a single Document for
            text/markdown/docx files.

        Raises:
            ValueError: If file type not supported or loading fails
        """
        try:
            # PDF — one Document per page, metadata includes "page"
            if file_type == "application/pdf":
                return PyPDFLoader(file_path).load()

            # Plain text / Markdown (plain-text loader handles .md fine)
            elif file_type in ("text/plain", "text/markdown"):
                return TextLoader(file_path).load()

            # Word documents
            elif file_type in (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/msword",
            ):
                return Docx2txtLoader(file_path).load()

            else:
                raise ValueError(f"Unsupported file type: {file_type}")

        except Exception as e:
            raise ValueError(f"Failed to load document: {str(e)}")

    @staticmethod
    def load_document_text(file_path: str, file_type: str) -> str:
        """
        Extract text from document based on file type.

        Args:
            file_path: Path to the file
            file_type: MIME type of the file

        Returns:
            Extracted text content

        Raises:
            ValueError: If file type not supported or loading fails
        """
        pages = DocumentProcessingService.load_document_pages(file_path, file_type)
        return "\n\n".join(page.page_content for page in pages)

    @staticmethod
    def chunk_text(
        text: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Split text into chunks for embedding.

        Args:
            text: Text to chunk
            chunk_size: Size of each chunk (characters)
            chunk_overlap: Overlap between chunks
            separators: Custom separators (optional)

        Returns:
            List of text chunks

        Example:
            chunks = DocumentProcessingService.chunk_text(
                text=document_text,
                chunk_size=1000,
                chunk_overlap=200
            )
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators or DocumentProcessingService.DEFAULT_SEPARATORS,
            length_function=len,
        )

        chunks = splitter.split_text(text)
        return chunks

    @staticmethod
    def chunk_documents(
        documents: List[Document],
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: Optional[List[str]] = None,
    ) -> List[Document]:
        """
        Split LangChain Documents into chunks, propagating each source
        document's metadata (e.g. PDF page number) onto every chunk split
        from it, plus a ``start_index`` recording where in that source
        page/section each chunk begins.

        Args:
            documents: Documents to split (e.g. from ``load_document_pages``)
            chunk_size: Size of each chunk (characters)
            chunk_overlap: Overlap between chunks
            separators: Custom separators (optional)

        Returns:
            List of chunked Documents, each with page_content + metadata

        Example:
            pages = DocumentProcessingService.load_document_pages(path, file_type)
            chunks = DocumentProcessingService.chunk_documents(pages)
            chunks[0].metadata  # {"source": ..., "page": 0, "start_index": 0}
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators or DocumentProcessingService.DEFAULT_SEPARATORS,
            length_function=len,
            add_start_index=True,
        )

        return splitter.split_documents(documents)

    @staticmethod
    def process_document(
        document_id: UUID,
        user_id: UUID,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> UserDocument:
        """
        Complete document processing pipeline.

        This is typically called from a Celery task for async processing.

        Args:
            document_id: UserDocument ID
            user_id: User ID (for permission check)
            chunk_size: Size of text chunks
            chunk_overlap: Overlap between chunks

        Returns:
            Updated UserDocument instance

        Raises:
            Exception: If processing fails (stored in error_message)

        Example:
            # In Celery task
            @shared_task
            def process_document_task(document_id, user_id):
                return DocumentProcessingService.process_document(
                    document_id=document_id,
                    user_id=user_id
                )
        """
        from accounts.models import CustomUser
        from ..models import UserDocument
        from ..services.vector_storage_service import VectorStorageService

        # Get document and user
        doc = UserDocument.objects.get(id=document_id, user_id=user_id)
        user = CustomUser.objects.get(id=user_id)

        try:
            # Update status
            doc.processing_status = "processing"
            doc.save()

            # Get file path
            file_path = doc.file.path

            # Extract text
            text = DocumentProcessingService.load_document_text(
                file_path=file_path, file_type=doc.file_type
            )

            # Chunk text
            chunks = DocumentProcessingService.chunk_text(
                text=text, chunk_size=chunk_size, chunk_overlap=chunk_overlap
            )

            # Store embeddings
            vector_ids = VectorStorageService.store_document_embeddings(
                document=doc, chunks=chunks, user=user
            )

            # Document is already marked as completed in store_document_embeddings

            return doc

        except Exception as e:
            # Mark as failed
            doc.processing_status = "failed"
            doc.error_message = str(e)
            doc.save()
            raise

    @staticmethod
    def reprocess_document(
        document_id: UUID,
        user_id: UUID,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> UserDocument:
        """
        Reprocess a document with different settings.

        Args:
            document_id: UserDocument ID
            user_id: User ID
            chunk_size: New chunk size (optional)
            chunk_overlap: New overlap (optional)

        Returns:
            Updated UserDocument instance

        Example:
            # Reprocess with larger chunks
            doc = DocumentProcessingService.reprocess_document(
                document_id=doc.id,
                user_id=user.id,
                chunk_size=2000
            )
        """
        from accounts.models import CustomUser
        from ..models import UserDocument
        from ..services.vector_storage_service import VectorStorageService

        doc = UserDocument.objects.get(id=document_id, user_id=user_id)
        user = CustomUser.objects.get(id=user_id)

        # Extract text
        file_path = doc.file.path
        text = DocumentProcessingService.load_document_text(
            file_path=file_path, file_type=doc.file_type
        )

        # Chunk with new settings
        chunks = DocumentProcessingService.chunk_text(
            text=text, chunk_size=chunk_size or 1000, chunk_overlap=chunk_overlap or 200
        )

        # Reindex
        VectorStorageService.reindex_document(
            document=doc, new_chunks=chunks, user=user
        )

        return doc

    @staticmethod
    def get_processing_status(document_id: UUID) -> Dict[str, Any]:
        """
        Get processing status for a document.

        Args:
            document_id: UserDocument ID

        Returns:
            Status information dict

        Example:
            status = DocumentProcessingService.get_processing_status(doc.id)
            if status['status'] == 'completed':
                print(f"Created {status['chunk_count']} chunks")
        """
        from ..models import UserDocument

        doc = UserDocument.objects.get(id=document_id)

        return {
            "document_id": str(doc.id),
            "file_name": doc.file_name,
            "status": doc.processing_status,
            "chunk_count": doc.chunk_count,
            "has_embeddings": doc.has_embeddings,
            "error_message": doc.error_message,
            "created_at": doc.created_at,
            "processed_at": doc.processed_at,
        }

    @staticmethod
    def delete_document(
        document_id: UUID, user_id: UUID, delete_file: bool = True
    ) -> None:
        """
        Delete document and its embeddings.

        Args:
            document_id: UserDocument ID
            user_id: User ID (for permission check)
            delete_file: Also delete the file from storage

        Example:
            DocumentProcessingService.delete_document(
                document_id=doc.id,
                user_id=user.id,
                delete_file=True
            )
        """
        from ..models import UserDocument
        from ..services.vector_storage_service import VectorStorageService

        doc = UserDocument.objects.get(id=document_id, user_id=user_id)

        # Delete embeddings first
        if doc.has_embeddings:
            VectorStorageService.delete_document_embeddings(doc)

        # Delete file if requested
        if delete_file and doc.file:
            file_path = doc.file.path
            if os.path.exists(file_path):
                os.remove(file_path)

        # Delete database record
        doc.delete()

    @staticmethod
    def get_document_preview(document_id: UUID, max_length: int = 500) -> str:
        """
        Get preview of document content.

        Args:
            document_id: UserDocument ID
            max_length: Max characters to return

        Returns:
            Preview text

        Example:
            preview = DocumentProcessingService.get_document_preview(
                document_id=doc.id,
                max_length=200
            )
        """
        from ..models import UserDocument

        doc = UserDocument.objects.get(id=document_id)

        if not doc.file:
            return ""

        try:
            text = DocumentProcessingService.load_document_text(
                file_path=doc.file.path, file_type=doc.file_type
            )

            if len(text) > max_length:
                return text[:max_length] + "..."
            return text
        except:
            return "Preview not available"

    @staticmethod
    def validate_file(
        uploaded_file: UploadedFile, max_size_mb: int = 10
    ) -> Dict[str, Any]:
        """
        Validate uploaded file before processing.

        Args:
            uploaded_file: Django UploadedFile object
            max_size_mb: Maximum file size in MB

        Returns:
            Validation result dict

        Example:
            result = DocumentProcessingService.validate_file(
                uploaded_file=request.FILES['document'],
                max_size_mb=10
            )

            if not result['valid']:
                return Response(result, status=400)
        """
        errors = []

        # Check file type
        file_type = uploaded_file.content_type
        if file_type not in DocumentProcessingService.SUPPORTED_TYPES:
            errors.append(f"Unsupported file type: {file_type}")

        # Check file size
        max_bytes = max_size_mb * 1024 * 1024
        if uploaded_file.size > max_bytes:
            errors.append(
                f"File too large: {uploaded_file.size / (1024*1024):.2f}MB. "
                f"Maximum: {max_size_mb}MB"
            )

        # Check file name
        if not uploaded_file.name:
            errors.append("File name is required")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "file_name": uploaded_file.name,
            "file_type": file_type,
            "file_size_mb": uploaded_file.size / (1024 * 1024),
        }


# ---------------------------------------------------------------------- #
#  Standalone CLI demo                                                     #
#                                                                            #
#      python document_processing_service.py path/to/file.pdf               #
#                                                                            #
#  Loads a text/pdf/docx file exactly the way the Celery pipeline does,     #
#  splits it with the same RecursiveCharacterTextSplitter, and prints the   #
#  resulting chunks, their overlaps, and their metadata with `rich` so you  #
#  can *see* what the splitter actually produces instead of imagining it.   #
#                                                                            #
#  No Django/DB/API key needed — this only exercises the loader + splitter. #
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import sys

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.traceback import install as install_rich_traceback

    install_rich_traceback(show_locals=False)
    console = Console()

    EXT_TO_MIME = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }

    parser = argparse.ArgumentParser(
        description=(
            "Load + chunk a document and inspect the chunks, overlaps, and "
            "metadata that RecursiveCharacterTextSplitter produces."
        ),
    )
    parser.add_argument(
        "file_path", help="Path to a .txt, .md, .pdf, .doc or .docx file"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Characters per chunk (default: 1000)",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=200,
        help="Overlap between consecutive chunks, in characters (default: 200)",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=160,
        help="How many characters of each chunk to show in the table (default: 160)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.file_path):
        console.print(f"[bold red]File not found:[/bold red] {args.file_path}")
        sys.exit(1)

    guessed_type, _ = mimetypes.guess_type(args.file_path)
    file_type = guessed_type or EXT_TO_MIME.get(
        os.path.splitext(args.file_path)[1].lower()
    )

    if file_type not in DocumentProcessingService.SUPPORTED_TYPES:
        console.print(
            Panel(
                f"Unsupported file type for [bold]{args.file_path}[/bold]\n"
                f"Supported extensions: {', '.join(EXT_TO_MIME)}",
                title="Unsupported file",
                border_style="red",
            )
        )
        sys.exit(1)

    console.rule("[bold cyan]Document Processing Demo[/bold cyan]")

    # ---- Load (metadata-preserving) then chunk -------------------------- #
    pages = DocumentProcessingService.load_document_pages(args.file_path, file_type)
    chunks = DocumentProcessingService.chunk_documents(
        pages,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )

    total_source_chars = sum(len(p.page_content) for p in pages)
    total_chunk_chars = sum(len(c.page_content) for c in chunks)
    lengths = [len(c.page_content) for c in chunks]

    # ---- Overview panel --------------------------------------------------#
    overview = Table.grid(padding=(0, 2))
    overview.add_row("[bold]File:[/bold]", os.path.basename(args.file_path))
    overview.add_row("[bold]Type:[/bold]", file_type)
    overview.add_row("[bold]Source pages/sections:[/bold]", str(len(pages)))
    overview.add_row("[bold]Total characters:[/bold]", f"{total_source_chars:,}")
    overview.add_row(
        "[bold]chunk_size / chunk_overlap:[/bold]",
        f"{args.chunk_size} / {args.chunk_overlap}",
    )
    overview.add_row("[bold]Chunks produced:[/bold]", str(len(chunks)))
    console.print(Panel(overview, title="Overview", border_style="cyan"))

    # ---- Raw metadata, straight from the LangChain loader ----------------#
    # Shown once (it's the same producer/creator/total_pages/etc. for every
    # page of one file) rather than repeated on every chunk row below.
    sample_meta = pages[0].metadata if pages else {}
    meta_lines = "\n".join(f"[bold]{k}[/bold]: {v!r}" for k, v in sample_meta.items())
    console.print(
        Panel(
            meta_lines or "(none)",
            title="Document metadata (as provided by the LangChain loader)",
            border_style="blue",
        )
    )

    # ---- Chunks table ------------------------------------------------------#
    chunk_table = Table(
        title=f"Chunks ({len(chunks)})", show_lines=True, header_style="bold magenta"
    )
    chunk_table.add_column("#", justify="right")
    chunk_table.add_column("Page", justify="right")
    chunk_table.add_column("Start idx", justify="right")
    chunk_table.add_column("Len", justify="right")
    chunk_table.add_column("Preview")

    for i, chunk in enumerate(chunks):
        preview = chunk.page_content.replace("\n", " ⏎ ")
        if len(preview) > args.preview_chars:
            preview = preview[: args.preview_chars] + "…"
        chunk_table.add_row(
            str(i),
            str(chunk.metadata.get("page", "-")),
            str(chunk.metadata.get("start_index", "-")),
            str(len(chunk.page_content)),
            preview,
        )

    console.print(chunk_table)

    # ---- Overlap table: exact overlap derived from start_index -----------#
    overlap_table = Table(
        title="Overlap between consecutive chunks", header_style="bold yellow"
    )
    overlap_table.add_column("Pair")
    overlap_table.add_column("Overlap chars", justify="right")
    overlap_table.add_column("Overlapping text")

    for i in range(len(chunks) - 1):
        cur, nxt = chunks[i], chunks[i + 1]
        same_source = cur.metadata.get("source") == nxt.metadata.get(
            "source"
        ) and cur.metadata.get("page") == nxt.metadata.get("page")
        cur_start = cur.metadata.get("start_index")
        nxt_start = nxt.metadata.get("start_index")

        if same_source and cur_start is not None and nxt_start is not None:
            overlap_len = max(0, (cur_start + len(cur.page_content)) - nxt_start)
            overlap_text = cur.page_content[-overlap_len:] if overlap_len else ""
            overlap_table.add_row(
                f"{i} → {i + 1}",
                str(overlap_len),
                repr(overlap_text[:80]) if overlap_text else "[dim]none[/dim]",
            )
        else:
            overlap_table.add_row(
                f"{i} → {i + 1}", "—", "[dim](new page/section — no overlap)[/dim]"
            )

    console.print(overlap_table)

    # ---- Summary panel -----------------------------------------------------#
    summary = Table.grid(padding=(0, 2))
    summary.add_row("[bold]Total chunks:[/bold]", str(len(chunks)))
    if lengths:
        summary.add_row(
            "[bold]Avg chunk length:[/bold]", f"{sum(lengths) / len(lengths):.1f} chars"
        )
        summary.add_row(
            "[bold]Min / Max chunk length:[/bold]",
            f"{min(lengths)} / {max(lengths)} chars",
        )
    if total_source_chars:
        overhead = total_chunk_chars - total_source_chars
        summary.add_row(
            "[bold]Overlap overhead:[/bold]",
            f"{overhead:,} chars ({overhead / total_source_chars * 100:.1f}% larger than the source text)",
        )
    console.print(Panel(summary, title="Summary", border_style="green"))
