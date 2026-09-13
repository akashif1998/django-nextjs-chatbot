# PGVector Collection & Metadata - Implementation Summary

## What Was Added (October 2025)

Based on official PGVector documentation research, I've added essential fields to the `UserDocument` model for proper pgvector integration.

## Changes Made

### 1. New Fields in `UserDocument` Model

**`vector_collection_name`** (CharField, Required)
```python
vector_collection_name = models.CharField(
    max_length=255,
    blank=True,
    null=True,
    db_index=True,
    help_text=_("PGVector collection name where embeddings are stored (REQUIRED for vector operations)")
)
```

**`vector_collection_metadata`** (JSONField, Optional)
```python
vector_collection_metadata = models.JSONField(
    default=dict,
    blank=True,
    help_text=_("Optional metadata for the PGVector collection itself")
)
```

**`vector_metadata`** (JSONField, Essential for Filtering)
```python
vector_metadata = models.JSONField(
    default=dict,
    blank=True,
    help_text=_(
        "Searchable metadata for pgvector filtering (e.g., {'user_id': '123', 'category': 'research'})"
    )
)
```

### 2. New Indexes

```python
models.Index(fields=['vector_collection_name'], name='userdoc_collection_idx'),
models.Index(fields=['user', 'vector_collection_name'], name='userdoc_user_collection_idx'),
```

### 3. Updated Methods

**Updated `mark_processing_completed()`:**
```python
def mark_processing_completed(
    self, 
    collection_name,          # NEW: Required
    vector_ids, 
    chunk_count, 
    collection_metadata=None, # NEW: Optional
    vector_metadata=None      # NEW: Optional
):
    self.vector_collection_name = collection_name
    # ... store metadata
```

**New `get_vector_metadata()` Method:**
```python
def get_vector_metadata(self):
    """Get metadata dict to be stored with vector embeddings."""
    return {
        "user_id": str(self.user.id),
        "document_id": str(self.id),
        "file_name": self.file_name,
        # ... more metadata
    }
```

**New `has_embeddings` Property:**
```python
@property
def has_embeddings(self):
    """Check if document has been processed and has embeddings."""
    return bool(
        self.processing_status == "completed" 
        and self.vector_collection_name 
        and self.vector_store_ids
    )
```

**New `get_documents_in_collection()` Class Method:**
```python
@classmethod
def get_documents_in_collection(cls, collection_name, user=None):
    """Get all documents in a specific PGVector collection."""
    # Returns QuerySet filtered by collection
```

## Why This Is Important

### From PGVector Documentation:

1. **Collection Name is REQUIRED**
   - Source: LangChain PGVector docs
   - `collection_name` is a fundamental parameter
   - It's NOT the table name - it's the namespace for embeddings
   - Allows multiple collections in same database
   - Default is "langchain" but should be customized

2. **Metadata Enables Filtering**
   - Source: PGVector filtering documentation  
   - Each document can have metadata stored as JSONB
   - Used for efficient similarity search filtering
   - Examples: `{"user_id": "123", "category": "research", "tags": ["ai", "ml"]}`

3. **Collection Metadata is Optional**
   - Source: PGVector API reference
   - Stores metadata about the collection itself
   - Different from document-level metadata

## Usage Examples

### Creating a Document Collection

```python
from langchain_postgres import PGVector
from langchain_openai import OpenAIEmbeddings

# 1. Upload document
doc = UserDocument.objects.create(
    user=user,
    file=uploaded_file,
    file_name="research.pdf"
)

# 2. Create collection name
collection_name = f"user_{user.id}_documents"

# 3. Initialize PGVector
vector_store = PGVector(
    embeddings=OpenAIEmbeddings(),
    collection_name=collection_name,  # REQUIRED!
    connection=settings.PGVECTOR_CONNECTION_STRING,
    use_jsonb=True
)

# 4. Get searchable metadata
metadata = doc.get_vector_metadata()
# Returns: {
#     "user_id": "123",
#     "document_id": "456",
#     "file_name": "research.pdf",
#     "file_type": "application/pdf",
#     "tags": ["research", "ai"]
# }

# 5. Process and store
chunks = text_splitter.split_text(text)
vector_ids = vector_store.add_texts(
    texts=chunks,
    metadatas=[metadata] * len(chunks)
)

# 6. Save to Django model
doc.mark_processing_completed(
    collection_name=collection_name,
    vector_ids=vector_ids,
    chunk_count=len(chunks),
    collection_metadata={"user_id": str(user.id)},
    vector_metadata=metadata
)
```

### Searching with Metadata Filters

```python
# Initialize vector store with collection
vector_store = PGVector(
    embeddings=OpenAIEmbeddings(),
    collection_name=doc.vector_collection_name,  # From Django model
    connection=settings.PGVECTOR_CONNECTION_STRING
)

# Search with user filter
results = vector_store.similarity_search(
    query="What is machine learning?",
    k=5,
    filter={"user_id": {"$eq": str(user.id)}}  # Only this user's docs
)

# Search with category filter
results = vector_store.similarity_search(
    query="Python tutorials",
    k=3,
    filter={"category": {"$eq": "programming"}}
)

# Complex filter
results = vector_store.similarity_search(
    query="AI research",
    k=10,
    filter={
        "$and": [
            {"user_id": {"$eq": str(user.id)}},
            {"tags": {"$contains": "research"}}
        ]
    }
)
```

### Multi-User Document Organization

```python
# Each user has their own collection
user_collection = f"user_{user.id}_documents"

# Session-specific collections
session_collection = f"session_{session.id}_context"

# Category-based collections
research_collection = "research_papers"
code_collection = "code_snippets"

# Get all documents in user's collection
user_docs = UserDocument.get_documents_in_collection(
    collection_name=user_collection,
    user=user
)
```

## Database Schema Changes

When you run migrations, these fields will be added:

```sql
ALTER TABLE chatbot_userdocument 
ADD COLUMN vector_collection_name VARCHAR(255) NULL,
ADD COLUMN vector_collection_metadata JSONB DEFAULT '{}',
ADD COLUMN vector_metadata JSONB DEFAULT '{}';

CREATE INDEX userdoc_collection_idx 
ON chatbot_userdocument (vector_collection_name);

CREATE INDEX userdoc_user_collection_idx 
ON chatbot_userdocument (user_id, vector_collection_name);
```

## Migration Steps

```bash
# 1. Create migration
python manage.py makemigrations chatbot

# 2. Review migration file
# Check: apps/chatbot/migrations/000X_add_vector_collection.py

# 3. Apply migration
python manage.py migrate chatbot

# 4. Update existing documents (if any)
python manage.py shell
>>> from apps.chatbot.models import UserDocument
>>> docs = UserDocument.objects.filter(processing_status='completed')
>>> for doc in docs:
...     if not doc.vector_collection_name:
...         doc.vector_collection_name = f"user_{doc.user.id}_legacy"
...         doc.save()
```

## Best Practices

### Collection Naming Convention

```python
# ✅ Good: Descriptive and unique
f"user_{user.id}_documents"
f"session_{session.id}_context" 
f"org_{org.id}_knowledge_base"

# ❌ Bad: Generic or unclear
"documents"
"my_docs"
"data"
```

### Metadata Structure

```python
# ✅ Good: Structured and searchable
{
    "user_id": "123",
    "document_id": "456",
    "category": "research",
    "tags": ["ai", "ml", "python"],
    "date": "2025-01-15",
    "language": "en"
}

# ❌ Bad: Too nested or unstructured
{
    "data": {
        "info": {
            "user": {"id": "123"}  # Too nested
        }
    }
}
```

### Filtering Queries

```python
# ✅ Good: Use indexed metadata fields
filter={"user_id": {"$eq": str(user.id)}}

# ✅ Good: Combine filters efficiently
filter={
    "$and": [
        {"user_id": {"$eq": str(user.id)}},
        {"category": {"$eq": "research"}}
    ]
}

# ❌ Avoid: Non-indexed or complex nested queries
filter={"data.info.user.id": {"$eq": "123"}}
```

## Testing

```python
# tests/test_vector_storage.py
from django.test import TestCase
from apps.chatbot.models import UserDocument

class VectorStorageTests(TestCase):
    def test_collection_name_required(self):
        """Test that collection name is stored."""
        doc = UserDocument.objects.create(...)
        doc.mark_processing_completed(
            collection_name="test_collection",
            vector_ids=["id1", "id2"],
            chunk_count=2
        )
        self.assertEqual(doc.vector_collection_name, "test_collection")
    
    def test_vector_metadata_generation(self):
        """Test metadata generation."""
        doc = UserDocument.objects.create(...)
        metadata = doc.get_vector_metadata()
        
        self.assertIn("user_id", metadata)
        self.assertIn("document_id", metadata)
        self.assertEqual(metadata["user_id"], str(doc.user.id))
```

## References

- [LangChain PGVector Documentation](https://python.langchain.com/docs/integrations/vectorstores/pgvector/)
- [PGVector API Reference](https://api.python.langchain.com/en/latest/vectorstores/langchain_postgres.vectorstores.PGVector.html)
- [langchain-postgres Package](https://github.com/langchain-ai/langchain-postgres)

## Summary

✅ **Added**: Collection name and metadata fields
✅ **Reason**: Required by PGVector for proper document organization
✅ **Benefit**: Enable multi-user document filtering and efficient search
✅ **Impact**: Better RAG performance and user data isolation

The implementation follows official PGVector documentation patterns and enables production-ready RAG features.
