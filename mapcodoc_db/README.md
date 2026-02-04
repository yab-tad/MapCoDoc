# MapCoDoc Database

A SQLAlchemy-based database layer for storing and querying code analysis results and extracted documentation.

## Overview

The MapCoDoc database provides persistent storage for:
- **Code Analysis Results**: Modules, classes, functions, methods, and their relationships
- **API Resolution**: Public API names, export chains, and re-export tracking
- **Import/Export Relationships**: Module dependencies and visibility
- **Inheritance Tracking**: Inherited member relationships with derived API names
- **Documentation**: Extracted API reference documentation linked to code members

### Inherited Member Support

A key feature is tracking **inherited members** - methods that classes inherit from parent classes. This enables:

1. **Documentation Linking**: When docs reference `xgboost.XGBRFClassifier.evals_result`, the database can find it even though `evals_result` is defined in `XGBModel`

2. **Multi-level Inheritance**: Properly tracks inheritance chains (e.g., `XGBRFClassifier` → `XGBClassifier` → `XGBModel`)

3. **API Name Derivation**: Each inherited member has API names derived from the inheriting class's path

4. **Original Definition Linking**: Maintains references to the original member for source code/signature access

## Schema

### Entity-Relationship Diagram

```
┌─────────────┐       ┌─────────────┐       ┌─────────────┐
│  DBModule   │1─────*│  DBMember   │1─────*│ DBSignature │
│             │       │             │       │             │
│ - name      │       │ - fqn       │       │ - variant   │
│ - file_path │       │ - api_name  │       │ - text      │
│ - has_all   │       │ - type      │       └─────────────┘
│ - exports[] │       │ - parent_id │
└─────────────┘       │ - api_ref   │
       │              └──────┬──────┘
       │                     │
       │1                    │ 1 (inheriting_class)
       ▼                     ▼
┌─────────────┐       ┌───────────────────┐
│  DBImport   │       │ DBInheritedMember │
│             │       │                   │
│ - source    │       │ - member_name     │
│ - alias     │       │ - inherited_api   │
│ - is_rel    │       │ - original_api    │
└─────────────┘       │ - source_class    │
       │              │ - signature       │
       │              └─────────┬─────────┘
       │                        │ * (original_member)
       ▼                        ▼
┌─────────────┐       ┌─────────────┐
│  DBExport   │*─────1│  DBMember   │
│             │       │  (target)   │
│ - name      │       └─────────────┘
│ - is_reexp  │
└─────────────┘
```

**Inheritance Tracking:**
- `DBInheritedMember` links a class to methods it inherits from parent classes
- `inheriting_class_id` → the class that inherits the member
- `original_member_id` → the original definition (nullable if external)
- `inherited_api_name` → derived API path via inheriting class (indexed)

### Tables

#### `modules`
Represents Python source files/packages.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `name` | String | Dotted module path (e.g., `torch.nn.modules.conv`) |
| `file_path` | String | Relative path to source file |
| `is_package` | Boolean | True if `__init__.py` |
| `has_all` | Boolean | True if defines `__all__` |
| `all_exports` | JSON | List of names in `__all__` |
| `needs_dynamic_analysis` | Boolean | Whether dynamic analysis is needed |
| `module_statistics` | JSON | Stats like `{num_classes: 5, loc: 1200}` |

#### `members`
Represents code definitions (classes, functions, methods, variables).

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `module_id` | FK → modules | Defining module |
| `name` | String | Short name (e.g., `Conv1d`) |
| `fully_qualified_name` | String | Full path (e.g., `torch.nn.modules.conv.Conv1d`) |
| `member_type` | String | `class`, `function`, `method`, `variable` |
| `parent_id` | FK → members | Parent class for methods/nested classes |
| `primary_api_name` | String | Canonical public API name |
| `all_api_names` | JSON | List of all public names |
| `api_name_sources` | JSON | Map: API name → exporting module |
| `source_code` | Text | Full source code |
| `docstring` | Text | Inline docstring |
| `parameters` | JSON | `[{name, type, default}, ...]` |
| `returns` | JSON | `{type, description}` |
| `decorators` | JSON | List of decorator strings |
| `is_async` | Boolean | Async function/method |
| `is_static` | Boolean | Static method |
| `is_abstract` | Boolean | Abstract method |
| `is_property` | Boolean | Property decorator |
| **Documentation Fields** | | |
| `doc_source_type` | String | `pdf`, `web` - source type |
| `doc_source_path` | String | Source file/URL path |
| `doc_page_range` | String | PDF pages (e.g., `"10-12"`) |
| `doc_section_path` | String | Breadcrumb path |
| `doc_score` | Integer | Extraction confidence (0-100) |
| `doc_format` | String | `structured`, `raw`, or `None` - **distinguishes doc type** |
| `doc_raw_text` | Text | Full raw text (when `doc_format='raw'`) |
| `api_reference_file` | String | Path to doc file (.json or .txt) |
| `api_reference` | JSON | Structured JSON docs (when `doc_format='structured'`) |
| `doc_signature` | Text | Quick-access: signature from docs |
| `doc_description` | Text | Quick-access: description |
| `doc_examples` | JSON | Quick-access: code examples (structured only) |

#### `signatures`
Stores signature variations for members.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `member_id` | FK → members | Associated member |
| `variant` | String | `full`, `default`, `no_types` |
| `signature_text` | Text | The signature string |

#### `exports`
Captures module export relationships.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `exporter_module_id` | FK → modules | Module doing the export |
| `exported_name` | String | Name exported as |
| `target_member_id` | FK → members | Underlying definition (nullable) |
| `is_explicit` | Boolean | In `__all__` |
| `is_reexport` | Boolean | Re-exported from elsewhere |
| `is_wildcard` | Boolean | From `*` import |

#### `imports`
Captures import statements.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `importer_module_id` | FK → modules | Module doing the import |
| `line_number` | Integer | Source line |
| `source_module_fqn` | String | Imported from module |
| `imported_entity_fqn` | String | What was imported |
| `name_bound_in_importer` | String | Bound name (e.g., `np`) |
| `raw_alias` | String | Alias if `as` used |
| `is_relative` | Boolean | Relative import |
| `is_wildcard` | Boolean | `from x import *` |
| `is_source_internal` | Boolean | From same codebase |

#### `inherited_members`
Tracks inherited member relationships for documentation linking.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer | Primary key |
| `inheriting_class_id` | FK → members | Class that INHERITS the member |
| `original_member_id` | FK → members | Original definition (nullable if external) |
| `member_name` | String | Method name (e.g., `evals_result`) |
| `member_type` | String | `method`, `property`, etc. |
| `source_class_fqn` | String | Source class FQN (e.g., `xgboost.XGBModel`) |
| `original_fqn` | String | Original member FQN |
| `inherited_api_name` | String | Derived API name (indexed, e.g., `xgboost.XGBRFClassifier.evals_result`) |
| `inherited_api_names` | JSON | All derived API names |
| `original_api_name` | String | Original API name |
| `original_api_names` | JSON | All original API names |
| `signature` | JSON | Signature for stop signal matching |
| `is_external` | Boolean | True if source is external |
| **Documentation Fields (for external members)** | | |
| `doc_format` | String | `structured`, `raw`, or None |
| `doc_source_type` | String | `web` or `pdf` |
| `doc_source_path` | String | Path to source doc file |
| `doc_raw_text` | Text | Full raw documentation (when `doc_format='raw'`) |
| `api_reference` | JSON | Structured JSON docs (when `doc_format='structured'`) |
| `doc_signature` | String | Quick-access: signature from docs |
| `doc_description` | Text | Quick-access: description |

**Unique Constraint:** (`inheriting_class_id`, `member_name`) - A class can only inherit each member once.

**Note:** For external inherited members (`original_member_id` is NULL), documentation is stored directly on the `DBInheritedMember` record. For internal inherited members, documentation is retrieved from the original `DBMember` record.

---

## API Usage

### Initialization

```python
from mapcodoc_db import MapCoDocDB, QueryManager

# Initialize database
db = MapCoDocDB("mapcodoc_output/mapcodoc.db")
db.init_db()  # Creates tables. WARNING: reset=True by default, deletes existing DB!

# To preserve existing data:
db.init_db(reset=False)  # Only creates tables if they don't exist

# Get a session for queries
session = db.get_session()
qm = QueryManager(session)
```

**Important:** `init_db(reset=True)` (the default) will delete and recreate the database file. Use `reset=False` to preserve existing data.

### Ingesting Code Analysis Results

```python
import json

# Load analysis results
with open("mapcodoc_output/mapcodoc_results.json") as f:
    analysis_results = json.load(f)

# Ingest into database
# This automatically handles:
#   Phase 1: Modules and Members
#   Phase 1.5: Parent-child relationships
#   Phase 2: Export records
#   Phase 3: Import records
#   Phase 4: Inherited members (from inherited_methods in class components)
db.ingest_analysis_results(analysis_results)
```

**Inherited Member Ingestion (Phase 4):**

When a class component has an `inherited_methods` dict, the ingestion process:
1. Creates `DBInheritedMember` records linking the class to each inherited method
2. Stores derived API names (`inherited_api_name`, `inherited_api_names`)
3. Links to the original member if internal (`original_member_id`)
4. Preserves signatures for stop signal matching during doc extraction

### Ingesting Documentation

The database is updated automatically by `DocProcessingRunner._update_database()` or `_update_database_from_raw()`:

```python
# Structured docs (from postprocessed_doc/*.json)
# Sets doc_format='structured', stores in api_reference field
db._update_database()

# Raw docs (from per_member/*.txt, when skip_llm=True)  
# Sets doc_format='raw', stores in doc_raw_text field
db._update_database_from_raw(members)
```

For manual ingestion:

```python
# Option 1: Structured docs from a dictionary
db.ingest_documentation_results({
    "torch.nn.L1Loss": {
        "api_reference": {
            "module_member_signature": "...",
            "module_member_description": {"purpose": "..."},
            "parameters": [...],
            "examples": [...]
        },
        "doc_source_type": "web",  # 'web' or 'pdf'
        "doc_format": "structured",  # Mark as structured
        "doc_score": 95
    }
})

# Option 2: From a directory of JSON files
db.ingest_documentation_from_directory("docs/api_reference/")
```

### Querying Modules

```python
# List all modules
modules = qm.get_all_modules()

# Get module details
mod = qm.get_module_details("torch.nn")
print(f"Module: {mod.name}")
print(f"Members: {mod.member_count}, Exports: {mod.export_count}")

# Get packages only
packages = qm.get_packages()
```

### Querying Members

```python
# Get member by FQN
member = qm.get_member_details("torch.nn.modules.conv.Conv1d")
print(f"API Name: {member.api_name}")
print(f"Signatures: {member.signatures}")
print(f"Parameters: {member.parameters}")

# Get member by API name
member = qm.get_member_by_api_name("torch.nn.Conv1d")

# Get member by ANY API name
member = qm.get_member_by_any_api_name("torch.nn.parameter.Parameter")

# Get all methods of a class
methods = qm.get_class_methods("torch.nn.modules.conv.Conv1d")

# Get full class hierarchy (including nested classes)
hierarchy = qm.get_class_hierarchy("torch.nn.Module")
# Returns: {"class": ..., "methods": [...], "nested_classes": [...]}

# Search members
results = qm.search_members("Conv", limit=10)

# Get members by type
classes = qm.get_members_by_type("class", module_prefix="torch.nn")
functions = qm.get_members_by_type("function")

# Get all public members (those with API names)
public = qm.get_public_members(module_prefix="numpy")
```

### Querying Documentation

The database supports two documentation formats:
- **`structured`**: LLM-processed JSON with full structure (signature, parameters, examples, etc.)
- **`raw`**: Raw extracted text without LLM processing (when `skip_llm=True`)

```python
# Get documentation for a member
doc = qm.get_member_documentation("torch.nn.L1Loss")
print(f"Description: {doc.doc_description}")
print(f"Examples: {doc.doc_examples}")
print(f"Full API Reference: {doc.api_reference}")

# Get all documented members
documented = qm.get_members_with_documentation(module_prefix="torch")

# Get members missing documentation
missing = qm.get_members_without_documentation(module_prefix="torch")

# Get documentation coverage statistics
coverage = qm.get_documentation_coverage(module_prefix="torch.nn")
print(f"Coverage: {coverage['coverage_percentage']:.1f}%")
print(f"By type: {coverage['by_type']}")
```

### Format-Aware Documentation Queries

```python
# Get documentation with format awareness
doc = qm.get_member_documentation_by_format("torch.nn.L1Loss")
if doc['format'] == 'structured':
    # Use rich JSON structure
    params = doc['content'].get('parameters', [])
    examples = doc['examples']
    print(f"Parameters: {len(params)}")
elif doc['format'] == 'raw':
    # Raw text - parse manually if needed
    raw_text = doc['content']
    print(f"Raw text length: {len(raw_text)} chars")
else:
    print("No documentation available")

# Get documentation format statistics
stats = qm.get_documentation_format_statistics()
print(f"Structured: {stats['structured']}")
print(f"Raw: {stats['raw']}")
print(f"None: {stats['none']}")

# Get all members with a specific doc format
raw_members = qm.get_members_by_doc_format('raw')
structured_members = qm.get_members_by_doc_format('structured')
undocumented = qm.get_members_by_doc_format('none')

# Filter by module prefix
torch_raw = qm.get_members_by_doc_format('raw', module_prefix='torch.nn')
```

### Querying Inherited Members

```python
# Get all inherited members for a class
inherited = qm.get_inherited_members_for_class("xgboost.XGBRFClassifier")
for im in inherited:
    print(f"{im.member_name}: {im.inherited_api_name} (from {im.source_class_fqn})")

# Find an inherited member by its derived API name
# This is the KEY method for documentation linking
inherited = qm.get_inherited_member_by_api_name("xgboost.XGBRFClassifier.evals_result")
if inherited:
    print(f"Found: {inherited.inherited_api_name}")
    print(f"Original: {inherited.original_api_name}")
    print(f"Source class: {inherited.source_class_fqn}")

# Get the ORIGINAL member definition for an inherited member
original = qm.get_original_member_for_inherited("xgboost.XGBRFClassifier.evals_result")
if original:
    print(f"Original FQN: {original.fqn}")
    print(f"Signatures: {original.signatures}")

# COMPREHENSIVE lookup - checks direct AND inherited members
result = qm.find_member_by_any_path("xgboost.XGBRFClassifier.evals_result")
if result:
    if result['type'] == 'direct':
        print(f"Direct member: {result['member'].fqn}")
    else:
        print(f"Inherited: {result['member'].inherited_api_name}")
        print(f"Original: {result['original_member'].fqn if result['original_member'] else 'external'}")

# Get ALL possible API names for a member (including inherited paths)
all_names = qm.get_all_api_names_for_member("xgboost.core.XGBModel.evals_result")
# Returns: ['xgboost.XGBModel.evals_result', 'xgboost.XGBClassifier.evals_result', 
#           'xgboost.XGBRFClassifier.evals_result', ...]

# Get class hierarchy including inherited methods
hierarchy = qm.get_class_with_inherited_hierarchy("xgboost.XGBRFClassifier")
print(f"Direct methods: {len(hierarchy['methods'])}")
print(f"Inherited methods: {len(hierarchy['inherited_methods'])}")

# Get documentation for an inherited member (handles internal vs external)
doc = qm.get_inherited_member_documentation("xgboost.XGBClassifier.score")
if doc:
    print(f"Source: {doc['source']}")  # 'original_member' or 'inherited_member'
    print(f"Format: {doc['doc_format']}")
    print(f"External: {doc['is_external']}")
    if doc['api_reference']:
        # Structured documentation available
        params = doc['api_reference'].get('parameters', [])
    elif doc['doc_raw_text']:
        # Raw documentation
        print(doc['doc_raw_text'][:500])
```

### Querying Exports

```python
# Get all exports from a module (for stop signals)
exports = qm.get_public_peers("torch.nn")
for exp in exports:
    print(f"{exp.exported_name} -> {exp.target_fqn} -> {exp.target_api_name} -> {exp.signatures}")

# Find where a member is exported
exporting_modules = qm.get_exporting_modules_for_member("torch.nn.modules.conv.Conv1d")
# Returns: ['torch.nn', 'torch.nn.modules', ...]

# Get all export records for a member
all_exports = qm.get_all_exports_for_member("torch.nn.modules.conv.Conv1d")
```

### Querying Imports

```python
# Get all imports for a module
imports = qm.get_module_imports("torch.nn.modules.conv")
for imp in imports:
    print(f"Line {imp.line_number}: {imp.name_bound} from {imp.source_module_fqn}")

# Get internal dependencies
internal_deps = qm.get_internal_dependencies("torch.nn.modules.conv")

# Get external dependencies
external_deps = qm.get_external_dependencies("torch.nn.modules.conv")

# Get reverse dependencies (who imports this module?)
dependents = qm.get_reverse_dependencies("torch.nn.functional")
```

### Statistics

```python
# Get overall database statistics
stats = qm.get_database_statistics()
print(f"Total modules: {stats['modules']['total']}")
print(f"Total members: {stats['members']['total']}")
print(f"  - Classes: {stats['members']['classes']}")
print(f"  - Functions: {stats['members']['functions']}")
print(f"  - Methods: {stats['members']['methods']}")
print(f"  - With API name: {stats['members']['with_api_name']}")
print(f"  - With docs: {stats['members']['with_documentation']}")
print(f"Inherited members: {stats['inherited_members']}")

# Get documentation FORMAT statistics
doc_stats = qm.get_documentation_format_statistics()
print(f"Structured docs: {doc_stats['structured']}")
print(f"Raw docs: {doc_stats['raw']}")
print(f"No docs: {doc_stats['none']}")

# Get undocumented public API
undocumented = qm.get_undocumented_public_api()
for api_name, fqn, member_type in undocumented:
    print(f"Missing docs: {api_name} ({member_type})")
```

### Batch Processing

```python
# Get all members for doc processing
members = qm.get_members_for_doc_processing("torch.nn")
for m in members:
    print(f"{m.api_name}: {m.signatures[0] if m.signatures else 'no sig'}")
```

---

## Data Classes

### Query Result Data Classes

**`MemberDetails`** - Comprehensive details for a code member (class, function, method, variable)

**`InheritedMemberDetails`** - Details for an inherited member relationship

```python
@dataclass
class InheritedMemberDetails:
    id: int                          # Database ID
    member_name: str                  # Method name (e.g., 'evals_result')
    member_type: str                  # 'method', 'property', etc.
    
    # Inheriting class info
    inheriting_class_id: int          # DB ID of inheriting class
    inheriting_class_fqn: str         # e.g., 'xgboost.XGBRFClassifier'
    inheriting_class_api_name: str    # e.g., 'xgboost.XGBRFClassifier'
    
    # Derived API names (via inheriting class)
    inherited_api_name: str           # e.g., 'xgboost.XGBRFClassifier.evals_result'
    inherited_api_names: List[str]    # All derived API names
    
    # Original member info
    original_member_id: int           # DB ID of original member (None if external)
    source_class_fqn: str             # e.g., 'xgboost.XGBModel'
    original_fqn: str                 # e.g., 'xgboost.XGBModel.evals_result'
    original_api_name: str            # Original API name
    original_api_names: List[str]     # All original API names
    
    # Metadata
    signature: Dict                   # Signature for stop signal matching
    is_external: bool                 # True if source is external to codebase
    
    # Documentation fields (for external inherited members)
    doc_format: str                   # 'structured', 'raw', or None
    doc_source_type: str              # 'web' or 'pdf'
    doc_source_path: str              # Path to source doc file
    doc_raw_text: str                 # Full raw documentation
    api_reference: Dict               # Structured JSON documentation
    doc_signature: str                # Quick-access: signature
    doc_description: str              # Quick-access: description
```

**`MemberDocumentation`** - Documentation details for a member

```python
@dataclass
class MemberDocumentation:
    member_id: int
    member_fqn: str
    member_api_name: str
    # Format indicator
    doc_format: str = None           # 'structured', 'raw', or None
    # Source info
    doc_source_type: str = None      # 'pdf', 'web'
    doc_source_path: str = None
    doc_page_range: str = None
    doc_section_path: str = None
    doc_score: int = None
    # Content (format-dependent)
    api_reference_file: str = None
    api_reference: Dict = None       # Structured JSON (when format='structured')
    doc_raw_text: str = None         # Raw text (when format='raw')
    # Quick access fields
    doc_signature: str = None
    doc_description: str = None
    doc_examples: List[Dict] = None
```

**`ModuleDetails`**, **`ImportDetails`**, **`ExportDetails`** - See `query.py` for full definitions.

---

## Documentation Formats

The database supports two documentation formats, controlled by the `doc_format` field:

### Structured Documentation (`doc_format='structured'`)

When LLM processing is enabled, the `api_reference` field stores structured JSON:

```json
{
    "module_member_signature": "...",
    "module_member_description": {
        "purpose": "...",
        "additional_information": ["...", "..."]
    },
    "parameters": [
        {
            "name": "...",
            "type": "...",
            "description": "...",
            "additional_information": "N/A"
        }
    ],
    "attributes": [],
    "methods": [],
    "examples": [
        {
            "example": ">>> ...",
            "additional_information": "N/A"
        }
    ],
    "additional_notes": {
        "supplementary_information": ["..."],
        "edge_cases": []
    }
}
```

### Raw Documentation (`doc_format='raw'`)

When `skip_llm=True` is used during doc processing, the extracted text is stored as-is:

- `doc_format` = `'raw'`
- `doc_raw_text` = Full extracted text content
- `api_reference` = `None` (no structured JSON)
- `doc_description` = First ~2000 chars of raw text
- `doc_signature` = First line if it looks like a signature
- `doc_examples` = `None` (no parsed examples)

This enables faster processing when LLM structuring isn't needed, while still preserving the extracted documentation for later processing or manual review.

### Querying Both Formats

```python
# The get_member_documentation_by_format method handles both:
doc = qm.get_member_documentation_by_format("torch.nn.Conv1d")

# Returns a unified structure:
# {
#     'format': 'structured' | 'raw' | None,
#     'content': JSON dict | raw text string | None,
#     'signature': str | None,
#     'description': str | None,
#     'examples': list | [],
#     'source_type': 'web' | 'pdf',
#     'source_path': str
# }
```

---

## File Structure

```
mapcodoc_db/
├── __init__.py          # Package exports
├── db_models.py         # SQLAlchemy ORM models
├── db_manager.py        # Database connection & ingestion
├── query.py             # Query interface
└── README.md            # This file
```

---

## Best Practices

1. **Session Management**: Always close sessions after use
   ```python
   session = db.get_session()
   try:
       qm = QueryManager(session)
       # ... queries ...
   finally:
       session.close()
   ```

2. **Batch Operations**: Use batch ingestion methods for better performance
   ```python
   # Good: Single batch call
   db.ingest_analysis_results(all_results)
   
   # Avoid: Multiple individual calls
   for result in all_results:
       db.ingest_analysis_results({key: result})
   ```

3. **Documentation Lookup**: Prefer API names for documentation queries
   ```python
   # Preferred: Public API name
   doc = qm.get_member_documentation("torch.nn.Conv1d")
   
   # Also works: Implementation FQN
   doc = qm.get_member_documentation("torch.nn.modules.conv.Conv1d")
   ```

4. **Inherited Member Lookup**: Use `find_member_by_any_path` for comprehensive search
   ```python
   # RECOMMENDED: Checks both direct AND inherited members
   result = qm.find_member_by_any_path("xgboost.XGBRFClassifier.evals_result")
   
   # Less comprehensive: Only checks direct members
   member = qm.get_member_by_any_api_name("xgboost.XGBRFClassifier.evals_result")
   # Returns None because evals_result is inherited, not direct!
   ```

5. **Building Comprehensive Indexes**: Include inherited paths
   ```python
   # Get ALL API names a member can be accessed by
   all_names = qm.get_all_api_names_for_member("xgboost.core.XGBModel.evals_result")
   # Includes: original paths + all inherited paths
   ```

6. **Documentation Format Handling**: Use format-aware queries
   ```python
   # RECOMMENDED: Handle both structured and raw docs uniformly
   doc = qm.get_member_documentation_by_format("torch.nn.Conv1d")
   if doc['format'] == 'structured':
       # Rich JSON available
       params = doc['content'].get('parameters', [])
   elif doc['format'] == 'raw':
       # Raw text - may need parsing
       text = doc['content']
   
   # Check format statistics before processing
   stats = qm.get_documentation_format_statistics()
   if stats['raw'] > 0:
       print(f"Warning: {stats['raw']} members have raw (unstructured) docs")
   ```

7. **Processing Raw Docs Later**: Query raw docs for LLM processing
   ```python
   # Get all raw-documented members for batch LLM processing
   raw_members = qm.get_members_by_doc_format('raw')
   for m in raw_members:
       # Process with LLM and update to structured
       pass
   ```

8. **External Inherited Member Documentation**: For external inherited members (e.g., methods from sklearn), documentation is stored directly on the inherited member record
   ```python
   # For external methods, documentation is on the inherited record itself
   inherited = qm.get_inherited_member_by_api_name("xgboost.XGBClassifier.score")
   if inherited and inherited.is_external:
       # Documentation is on inherited record, not original_member
       doc = qm.get_inherited_member_documentation("xgboost.XGBClassifier.score")
       print(f"External doc: {doc['doc_description']}")
   ```
