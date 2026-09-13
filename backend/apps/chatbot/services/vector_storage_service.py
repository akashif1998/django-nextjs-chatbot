"""
Vector Storage Service

Handles PGVector operations for document embeddings and semantic search (RAG).
Uses the modern ``langchain_postgres`` PGEngine + PGVectorStore API.

Usage:
    from chatbot.services import VectorStorageService

    # Store document embeddings
    vector_ids = VectorStorageService.store_document_embeddings(
        document=doc,
        chunks=text_chunks,
        user=request.user
    )

    # Semantic search
    results = VectorStorageService.semantic_search(
        query="What is machine learning?",
        user=request.user,
        k=5
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Dict, Any, Optional
from uuid import UUID, uuid5
import threading

import psycopg
from django.conf import settings
from langchain_postgres import PGEngine, PGVectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_core.documents import Document

if TYPE_CHECKING:
    # Deferred so this module can also run as a standalone script (see
    # `__main__` below) without needing Django app registry ready at import
    # time — `django.setup()` is called explicitly before these are needed.
    from ..models import UserDocument
    from accounts.models import CustomUser

# text-embedding-3-small produces 1536-dimensional vectors
EMBEDDING_DIMENSION = 1536

# Fixed, arbitrary namespace for uuid5() below — what matters is only that
# it never changes, so the same (document, chunk_index, chunk_text) always
# derives the same id. See `_stable_chunk_ids`.
_ID_NAMESPACE = UUID("b471a0b0-0031-4fd0-bc6d-3697f6ad286b")


class VectorStorageService:
    """Service for managing vector embeddings and semantic search via PGEngine + PGVectorStore."""

    # Singleton engine — shared across all requests in a process
    _engine: Optional[PGEngine] = None
    _engine_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @classmethod
    def _get_engine(cls) -> PGEngine:
        """
        Get or create the singleton PGEngine instance.

        PGEngine manages the SQLAlchemy connection pool. Reusing a single
        instance avoids opening a new pool on every request.

        Returns:
            PGEngine connected to ``settings.PGVECTOR_CONNECTION_STRING``
        """
        if cls._engine is None:
            with cls._engine_lock:
                # Double-checked locking
                if cls._engine is None:
                    cls._engine = PGEngine.from_connection_string(
                        url=settings.PGVECTOR_CONNECTION_STRING,
                    )
        return cls._engine

    @classmethod
    def _get_embedding_model(cls) -> OpenAIEmbeddings:
        """Return the default embedding model (text-embedding-3-small)."""
        return OpenAIEmbeddings(model="text-embedding-3-small")

    @classmethod
    def _table_exists(cls, table_name: str) -> bool:
        """
        Check whether *table_name* already exists in the ``public`` schema.

        ``PGEngine.init_vectorstore_table`` issues a bare ``CREATE TABLE`` —
        it does **not** check for an existing table first, and calling it
        again against a table that's already there raises
        ``psycopg.errors.DuplicateTable``. ``_get_vector_store`` below runs
        on every store/search/reindex call, so without this guard the
        *second* call ever made against a given user's table (the second
        document uploaded, or simply a search after one upload) would
        crash before doing anything.

        Args:
            table_name: The table to check for.

        Returns:
            True if the table already exists.
        """
        conn_str = settings.PGVECTOR_CONNECTION_STRING
        if conn_str.startswith("postgresql+psycopg://"):
            conn_str = conn_str.replace("postgresql+psycopg://", "postgresql://")

        with psycopg.connect(conn_str) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s);",
                (table_name,),
            )
            return bool(cur.fetchone()[0])

    @classmethod
    def _get_vector_store(
        cls,
        table_name: str,
        embeddings: Optional[Any] = None,
    ) -> PGVectorStore:
        """
        Create a PGVectorStore backed by *table_name*.

        The table is auto-created on first use via ``init_vectorstore_table``
        — but only when ``_table_exists`` says it isn't there yet.
        ``init_vectorstore_table`` is not itself idempotent (see
        ``_table_exists``'s docstring), so this method guards it explicitly
        rather than relying on the library to no-op safely.

        Args:
            table_name: PostgreSQL table name for the collection.
            embeddings: Embedding model override (default: text-embedding-3-small).

        Returns:
            A ready-to-use PGVectorStore instance.
        """
        engine = cls._get_engine()
        embedding_model = embeddings or cls._get_embedding_model()

        if not cls._table_exists(table_name):
            engine.init_vectorstore_table(
                table_name=table_name,
                vector_size=EMBEDDING_DIMENSION,
            )

        return PGVectorStore.create_sync(
            engine=engine,
            table_name=table_name,
            embedding_service=embedding_model,
        )

    # ------------------------------------------------------------------ #
    #  Collection / table naming helpers                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def create_user_collection_name(user: CustomUser) -> str:
        """
        Standardised table name for a user's documents.

        Args:
            user: The user

        Returns:
            Table name string

        Example:
            >>> VectorStorageService.create_user_collection_name(user)
            'user_123_documents'
        """
        return f"user_{user.id}_documents"

    @staticmethod
    def create_session_collection_name(session_id: UUID) -> str:
        """
        Standardised table name for a session's context.

        Args:
            session_id: Chat session ID

        Returns:
            Table name string

        Example:
            >>> VectorStorageService.create_session_collection_name(session.id)
            'session_abc123_context'
        """
        return f"session_{session_id}_context"

    # ------------------------------------------------------------------ #
    #  Write operations                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _stable_chunk_ids(document: UserDocument, chunks: List[str]) -> List[str]:
        """
        Deterministic vector-store ids for one document's chunks.

        Derived from ``(document.id, chunk_index, chunk_text)`` via
        ``uuid5``, so the same chunk always gets the same id.
        ``add_texts`` has no dedup of its own — call it twice without
        explicit ids and every chunk is inserted a second time under a
        fresh random UUID. Passing these in makes a retried call (a Celery
        task retried after a partial failure, e.g. embeddings stored but
        ``mark_processing_completed`` didn't finish) an upsert instead of a
        duplicate: same document, same chunks in, same ids out, existing
        rows overwritten in place.

        Args:
            document: The document the chunks belong to.
            chunks: The chunk texts about to be stored, in order.

        Returns:
            One UUID string per chunk, in the same order as *chunks*.
        """
        return [
            str(uuid5(_ID_NAMESPACE, f"{document.id}:{i}:{chunk}"))
            for i, chunk in enumerate(chunks)
        ]

    @staticmethod
    def store_document_embeddings(
        document: UserDocument,
        chunks: List[str],
        user: CustomUser,
        collection_name: Optional[str] = None,
        embeddings: Optional[Any] = None,
    ) -> List[str]:
        """
        Store document chunks as vector embeddings.

        Idempotent: ids are derived deterministically from
        ``(document.id, chunk_index, chunk_text)`` (see
        ``_stable_chunk_ids``), so calling this twice for the same
        document — a retried Celery task, for instance — upserts each
        chunk instead of duplicating it in the vector table.

        Args:
            document: UserDocument instance.
            chunks: Text chunks to embed.
            user: Owning user.
            collection_name: Custom table name (default: per-user table).
            embeddings: Custom embedding model override.

        Returns:
            List of stored vector IDs.

        Example:
            vector_ids = VectorStorageService.store_document_embeddings(
                document=doc,
                chunks=text_chunks,
                user=request.user,
            )
        """
        table_name = (
            collection_name or VectorStorageService.create_user_collection_name(user)
        )

        vector_store = VectorStorageService._get_vector_store(
            table_name=table_name,
            embeddings=embeddings,
        )

        metadata = document.get_vector_metadata()
        ids = VectorStorageService._stable_chunk_ids(document, chunks)

        vector_ids = vector_store.add_texts(
            texts=chunks,
            metadatas=[metadata] * len(chunks),
            ids=ids,
        )

        document.mark_processing_completed(
            collection_name=table_name,
            vector_ids=vector_ids,
            chunk_count=len(chunks),
            collection_metadata={"user_id": str(user.id)},
            vector_metadata=metadata,
        )

        return vector_ids

    @staticmethod
    def reindex_document(
        document: UserDocument,
        new_chunks: List[str],
        user: CustomUser,
    ) -> List[str]:
        """
        Reindex a document (delete old embeddings, then store new ones).

        Args:
            document: UserDocument to reindex.
            new_chunks: New text chunks.
            user: Document owner.

        Returns:
            New vector IDs.

        Example:
            new_chunks = splitter.split_text(text)
            VectorStorageService.reindex_document(doc, new_chunks, user)
        """
        VectorStorageService.delete_document_embeddings(document)
        return VectorStorageService.store_document_embeddings(
            document=document,
            chunks=new_chunks,
            user=user,
        )

    @staticmethod
    def delete_document_embeddings(document: UserDocument) -> None:
        """
        Delete all embeddings for a document.

        Args:
            document: UserDocument whose embeddings should be removed.

        Example:
            VectorStorageService.delete_document_embeddings(doc)
        """
        if not document.has_embeddings:
            return

        vector_store = VectorStorageService._get_vector_store(
            document.vector_collection_name,
        )

        # Batch-delete all vector IDs in one call
        vector_store.delete(ids=document.vector_store_ids)

        # Clear metadata on the model
        document.vector_collection_name = ""
        document.vector_store_ids = []
        document.chunk_count = 0
        document.save()

    # ------------------------------------------------------------------ #
    #  Search operations                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def semantic_search(
        query: str,
        user: CustomUser,
        k: int = 5,
        collection_name: Optional[str] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        embeddings: Optional[Any] = None,
    ) -> List[Document]:
        """
        Semantic similarity search over a user's documents.

        Args:
            query: Natural-language search query.
            user: User whose documents to search.
            k: Number of results.
            collection_name: Specific table (default: user table).
            filter_dict: Additional metadata filters.
            embeddings: Custom embedding model.

        Returns:
            List of matching Document objects.

        Example:
            results = VectorStorageService.semantic_search(
                query="What is machine learning?",
                user=request.user,
                k=5,
            )
        """
        table_name = (
            collection_name or VectorStorageService.create_user_collection_name(user)
        )

        vector_store = VectorStorageService._get_vector_store(
            table_name=table_name,
            embeddings=embeddings,
        )

        # Base filter: only this user's documents
        base_filter: Dict[str, Any] = {"user_id": {"$eq": str(user.id)}}
        search_filter = (
            {"$and": [base_filter, filter_dict]} if filter_dict else base_filter
        )

        return vector_store.similarity_search(query=query, k=k, filter=search_filter)

    @staticmethod
    def semantic_search_with_scores(
        query: str,
        user: CustomUser,
        k: int = 5,
        collection_name: Optional[str] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
    ) -> List[tuple[Document, float]]:
        """
        Semantic search with relevance scores.

        Args:
            query: Natural-language search query.
            user: User whose documents to search.
            k: Number of results.
            collection_name: Specific table (default: user table).
            filter_dict: Additional metadata filters.

        Returns:
            List of (Document, score) tuples ordered by relevance.

        Example:
            for doc, score in VectorStorageService.semantic_search_with_scores(
                query="AI research", user=request.user, k=10,
            ):
                print(f"Score: {score:.3f}  {doc.page_content[:100]}")
        """
        table_name = (
            collection_name or VectorStorageService.create_user_collection_name(user)
        )

        vector_store = VectorStorageService._get_vector_store(table_name=table_name)

        base_filter: Dict[str, Any] = {"user_id": {"$eq": str(user.id)}}
        search_filter = (
            {"$and": [base_filter, filter_dict]} if filter_dict else base_filter
        )

        return vector_store.similarity_search_with_score(
            query=query,
            k=k,
            filter=search_filter,
        )

    # ------------------------------------------------------------------ #
    #  Formatting & stats helpers                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def format_search_results_for_context(
        results: List[Document],
        max_context_length: Optional[int] = None,
    ) -> str:
        """
        Format search results into a context string suitable for LLM injection.

        Args:
            results: Documents from ``semantic_search()``.
            max_context_length: Truncate beyond this many characters.

        Returns:
            Formatted context string.

        Example:
            context = VectorStorageService.format_search_results_for_context(
                results, max_context_length=2000,
            )
        """
        context_parts = []

        for i, doc in enumerate(results, 1):
            source = doc.metadata.get("file_name", "Unknown")
            content = doc.page_content
            context_parts.append(f"[Source {i}: {source}]\n{content}\n")

        context = "\n".join(context_parts)

        if max_context_length and len(context) > max_context_length:
            context = context[:max_context_length] + "..."

        return context

    @staticmethod
    def get_user_storage_stats(user: CustomUser) -> Dict[str, Any]:
        """
        Aggregate storage statistics for a user.

        Args:
            user: The user

        Returns:
            Dict with total_documents, total_chunks, total_size_mb, etc.

        Example:
            stats = VectorStorageService.get_user_storage_stats(user)
        """
        from ..models import UserDocument

        user_docs = UserDocument.objects.filter(
            user=user,
            processing_status="completed",
        )

        total_docs = user_docs.count()
        total_chunks = sum(doc.chunk_count or 0 for doc in user_docs)
        total_size = sum(doc.file_size or 0 for doc in user_docs)

        collections = {
            doc.vector_collection_name
            for doc in user_docs
            if doc.vector_collection_name
        }

        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "collection_count": len(collections),
            "collections": list(collections),
        }

    @staticmethod
    def get_collection_documents(
        collection_name: str,
        user: Optional[CustomUser] = None,
    ) -> List[UserDocument]:
        """
        Get all UserDocument records belonging to a collection/table.

        Args:
            collection_name: Table name
            user: Optional user filter

        Returns:
            List of UserDocument instances
        """
        from ..models import UserDocument

        return UserDocument.get_documents_in_collection(
            collection_name=collection_name,
            user=user,
        )


# ---------------------------------------------------------------------- #
#  Standalone CLI demo                                                     #
#                                                                            #
#      python vector_storage_service.py path/to/file.pdf                    #
#                                                                            #
#  Chunks a file (reusing DocumentProcessingService), embeds the chunks     #
#  with the real OpenAIEmbeddings model, and — if Postgres is reachable —   #
#  round-trips them through the real PGVectorStore so you can see exactly   #
#  what gets stored and what similarity_search returns: vectors, metadata   #
#  (page numbers, source, chunk index), and relevance scores.               #
#                                                                            #
#  Reads OPENAI_API_KEY / PGVECTOR_CONNECTION_STRING the same way the       #
#  Django app does, by bootstrapping Django settings below — no manage.py   #
#  needed, just `python vector_storage_service.py <file>`.                  #
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse
    import mimetypes
    import os
    import re
    import sys
    import time
    from pathlib import Path

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.traceback import install as install_rich_traceback

    install_rich_traceback(show_locals=False)
    console = Console()

    parser = argparse.ArgumentParser(
        description=(
            "Chunk + embed a document and inspect the vectors/metadata "
            "that OpenAIEmbeddings + PGVectorStore produce."
        ),
    )
    parser.add_argument("file_path", help="Path to a .txt, .md, .pdf, .doc or .docx file")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--chunk-overlap", type=int, default=200)
    parser.add_argument(
        "--query", default="What is this document about?",
        help="Similarity-search query to run against the demo vector store",
    )
    parser.add_argument("--k", type=int, default=3, help="Number of search results to show")
    parser.add_argument(
        "--no-store", action="store_true",
        help="Only embed the chunks — skip the PGVectorStore round-trip",
    )
    args = parser.parse_args()

    if not os.path.exists(args.file_path):
        console.print(f"[bold red]File not found:[/bold red] {args.file_path}")
        sys.exit(1)

    # Bootstrap Django the same way manage.py does, so settings.* and the
    # OPENAI_API_KEY / PGVECTOR_CONNECTION_STRING env exports fire.
    import django

    BACKEND_DIR = Path(__file__).resolve().parents[3]
    for p in (str(BACKEND_DIR), str(BACKEND_DIR / "apps")):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from document_processing_service import DocumentProcessingService

    EXT_TO_MIME = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }
    guessed_type, _ = mimetypes.guess_type(args.file_path)
    file_type = guessed_type or EXT_TO_MIME.get(os.path.splitext(args.file_path)[1].lower())

    if file_type not in DocumentProcessingService.SUPPORTED_TYPES:
        console.print(f"[bold red]Unsupported file type for:[/bold red] {args.file_path}")
        sys.exit(1)

    console.rule("[bold cyan]Vector Storage Demo[/bold cyan]")

    # ---- Chunk (reusing the document-processing script's splitter) ------ #
    pages = DocumentProcessingService.load_document_pages(args.file_path, file_type)
    chunk_docs = DocumentProcessingService.chunk_documents(
        pages, chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap,
    )
    texts = [c.page_content for c in chunk_docs]
    metadatas = []
    for i, c in enumerate(chunk_docs):
        meta = dict(c.metadata)
        meta["chunk_index"] = i
        meta["source_file"] = os.path.basename(args.file_path)
        metadatas.append(meta)

    console.print(
        Panel(
            f"[bold]{len(texts)}[/bold] chunks from [bold]{os.path.basename(args.file_path)}[/bold] "
            f"(chunk_size={args.chunk_size}, chunk_overlap={args.chunk_overlap})",
            title="Chunks ready to embed",
            border_style="cyan",
        )
    )

    # ---- Embed ------------------------------------------------------------#
    embedding_model = VectorStorageService._get_embedding_model()

    with console.status("[bold green]Calling OpenAIEmbeddings..."):
        start = time.perf_counter()
        vectors = embedding_model.embed_documents(texts)
        elapsed = time.perf_counter() - start

    dim = len(vectors[0]) if vectors else 0

    embed_table = Table(title="Embeddings", header_style="bold magenta", show_lines=True)
    embed_table.add_column("#", justify="right")
    embed_table.add_column("Page", justify="right")
    embed_table.add_column("Chars", justify="right")
    embed_table.add_column("Dim", justify="right")
    embed_table.add_column("First 6 dims")

    for i, (vec, text) in enumerate(zip(vectors, texts)):
        sample = ", ".join(f"{v:.4f}" for v in vec[:6])
        embed_table.add_row(
            str(i), str(metadatas[i].get("page", "-")), str(len(text)), str(len(vec)), f"[{sample}, ...]",
        )

    console.print(embed_table)
    console.print(
        Panel(
            f"Model: text-embedding-3-small\n"
            f"Vectors: {len(vectors)}\n"
            f"Dimension: {dim}\n"
            f"Time: {elapsed:.2f}s ({elapsed / max(len(vectors), 1):.3f}s/chunk)",
            title="Embedding summary",
            border_style="green",
        )
    )

    if args.no_store:
        sys.exit(0)

    # ---- Round-trip through the real PGVectorStore -----------------------#
    table_name = "cli_demo_" + re.sub(r"[^a-z0-9]+", "_", Path(args.file_path).stem.lower()).strip("_")
    table_name = table_name.rstrip("_") or "cli_demo_document"
    ids = [str(uuid5(_ID_NAMESPACE, f"cli-demo:{table_name}:{i}:{t}")) for i, t in enumerate(texts)]

    try:
        with console.status(f"[bold green]Storing into PGVectorStore table '{table_name}'..."):
            vector_store = VectorStorageService._get_vector_store(
                table_name=table_name, embeddings=embedding_model,
            )
            vector_store.add_texts(texts=texts, metadatas=metadatas, ids=ids)

        console.print(
            Panel(f"Stored {len(texts)} vectors in table [bold]{table_name}[/bold]", border_style="green")
        )

        with console.status(f"[bold green]Running similarity_search_with_score(query={args.query!r})..."):
            results = vector_store.similarity_search_with_score(query=args.query, k=args.k)

        results_table = Table(
            title=f"Top {args.k} results for: {args.query!r}", header_style="bold magenta", show_lines=True,
        )
        results_table.add_column("Rank", justify="right")
        results_table.add_column("Score", justify="right")
        results_table.add_column("Page", justify="right")
        results_table.add_column("Chunk idx", justify="right")
        results_table.add_column("Content preview")

        for rank, (doc, score) in enumerate(results, 1):
            preview = doc.page_content.replace("\n", " ⏎ ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            results_table.add_row(
                str(rank),
                f"{score:.4f}",
                str(doc.metadata.get("page", "-")),
                str(doc.metadata.get("chunk_index", "-")),
                preview,
            )

        console.print(results_table)

        if results:
            top_meta = "\n".join(f"{k}: {v}" for k, v in results[0][0].metadata.items())
            console.print(
                Panel(
                    top_meta,
                    title="Metadata of top result (exactly what PGVectorStore returns)",
                    border_style="cyan",
                )
            )

    except Exception as e:
        console.print(
            Panel(
                f"Skipped the PGVectorStore round-trip — couldn't reach/write to Postgres:\n"
                f"[red]{e}[/red]\n\n"
                "The embeddings above are still real; this step just needs "
                "PGVECTOR_CONNECTION_STRING pointing at a running Postgres+pgvector instance.",
                title="Vector store round-trip skipped",
                border_style="yellow",
            )
        )
