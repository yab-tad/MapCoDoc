"""
Database models for MapCoDoc Code Analysis results.

This module defines the SQLAlchemy ORM schema for storing code components,
their hierarchies, signatures, and export relationships. It is designed
to support complex querying for documentation traceability.
"""

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text, JSON, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship, backref

Base = declarative_base()


class DBModule(Base):
    """
    Represents a Python source file or package.
    
    Attributes:
        id: Primary Key.
        name: Dotted module path (e.g., 'torch.nn.modules.conv').
        file_path: Relative path to the source file.
        is_package: True if this is an __init__.py file.
        has_all: True if the module defines __all__.
        all_exports: JSON list of strings found in __all__.
        needs_dynamic_analysis: True if the module needs dynamic analysis.
        dynamic_analysis_attempted: True if the dynamic analysis has been attempted.
        dynamic_analysis_success: True if the dynamic analysis was successful.
        module_statistics: JSON blob for module statistics.
    """
    __tablename__ = 'modules'
    
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True, nullable=False) # FQN e.g. 'torch.nn'
    file_path = Column(String, nullable=True)
    is_package = Column(Boolean, default=False)
    
    # Module Interface Info (from 'module_interface' key in analysis)
    has_all = Column(Boolean, default=False)
    all_exports = Column(JSON, nullable=True) # List of strings in __all__
    
    # Dynamic Analysis Metadata
    needs_dynamic_analysis = Column(Boolean, default=False)
    dynamic_analysis_attempted = Column(Boolean, default=False)
    dynamic_analysis_success = Column(Boolean, default=False)
    all_is_dynamic = Column(Boolean, default=False)  # True if __all__ was dynamically constructed
    
    # Module Statistics - JSON blob for flexibility
    # e.g., {"num_classes": 5, "num_functions": 10, "num_methods": 50, "loc": 1200}
    module_statistics = Column(JSON, nullable=True)
    
    # Relationships
    members = relationship("DBMember", back_populates="module", cascade="all, delete-orphan")
    exports = relationship("DBExport", back_populates="exporter_module", cascade="all, delete-orphan")
    imports = relationship("DBImport", back_populates="importer_module", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<DBModule(name='{self.name}')>"


class DBMember(Base):
    """
    Represents a specific code definition (Class, Function, Method, Variable).
    
    This table unifies different component types to simplify querying by module/hierarchy.
    It acts as the node in the 'Definition Graph'.
    """
    __tablename__ = 'members'
    
    id = Column(Integer, primary_key=True)
    module_id = Column(Integer, ForeignKey('modules.id'), nullable=False)
    
    # Identification
    name = Column(String, nullable=False)  # Short name (e.g., 'Conv1d', 'forward')
    fully_qualified_name = Column(String, unique=True, index=True, nullable=False) # e.g. 'torch.nn.modules.conv.Conv1d'
    member_type = Column(String, nullable=False) # 'class', 'function', 'method', 'variable'
    
    # Hierarchy (Adjacency List)
    # If type='method', parent is the Class. If nested class, parent is the outer Class.
    parent_id = Column(Integer, ForeignKey('members.id'), nullable=True)
    
    # Code Metadata
    source_start_line = Column(Integer, nullable=True)
    source_end_line = Column(Integer, nullable=True)
    docstring = Column(Text, nullable=True)
    source_code = Column(Text, nullable=True)
    
    # Public API Names and their sources
    primary_api_name = Column(String, nullable=True) # This is useful if FQN is internal but we want the 'canonical' name easily
    all_api_names = Column(JSON, nullable=True) # List of strings
    api_name_sources = Column(JSON, nullable=True) # Dict[str, str] -- Maps each API name to its exporting module FQN
    
    # Access & Visibility
    access_modifier = Column(String, default="public")  # 'public', 'protected', 'private'
    is_public = Column(Boolean, default=True)
    
    # Decorators
    decorators = Column(JSON, nullable=True)  # List of decorator strings, e.g., ["@staticmethod", "@property"]
    
    # Function/Method Characteristics
    is_async = Column(Boolean, default=False)
    is_static = Column(Boolean, default=False) # For methods
    is_abstract = Column(Boolean, default=False)
    is_override = Column(Boolean, default=False)
    is_property = Column(Boolean, default=False)
    property_type = Column(String, nullable=True)  # 'getter', 'setter', 'deleter'
    is_chain_candidate = Column(Boolean, default=False)  # returns self/cls
    
    # Parameters & Returns - structured JSON for rich querying
    # parameters: [{"name": "x", "type": "int", "default": "0"}, ...]
    parameters = Column(JSON, nullable=True)
    # returns: {"type": "int", "description": "..."}
    returns = Column(JSON, nullable=True)
    
    # ============ Documentation Fields ============
    # Source information (where the doc came from)
    doc_source_type = Column(String, nullable=True)  # 'pdf', 'html', 'rst', etc.
    doc_source_path = Column(String, nullable=True)  # Original file path or URL
    doc_page_range = Column(String, nullable=True)   # For PDF: "10-12", nullable for HTML
    doc_section_path = Column(String, nullable=True) # Breadcrumb: "Module > Class > Method"
    doc_score = Column(Integer, nullable=True)       # Confidence score of extraction
    # Documentation format indicator
    # 'structured' = LLM-processed JSON with full structure
    # 'raw' = Raw extracted text without LLM processing
    # None = No documentation
    doc_format = Column(String, nullable=True)  # 'structured', 'raw', or None
    doc_raw_text = Column(Text, nullable=True)  # Full raw text when doc_format='raw'
    api_reference_file = Column(String, nullable=True)  # Path to the .json file
    api_reference = Column(JSON, nullable=True)         # Or store the JSON content directly
    
    # Quick access fields (extracted from api_reference for querying)
    doc_signature = Column(Text, nullable=True)      # module_member_signature
    doc_description = Column(Text, nullable=True)    # module_member_description.purpose
    doc_examples = Column(JSON, nullable=True)       # examples array for quick lookup
    # ===============================================
    
    # Store the full export chain (list of dicts) for traceback/breadcrumbs
    best_export_chain = Column(JSON, nullable=True)
    
    # Relationships
    module = relationship("DBModule", back_populates="members")
    # Self-referential relationship for hierarchy (Class -> Methods)
    children = relationship("DBMember", 
                            backref=backref('parent', remote_side=[id]),
                            cascade="all, delete-orphan")
    
    signatures = relationship("DBSignature", back_populates="member", cascade="all, delete-orphan")
    # Reverse relationship for exports (where is this member exported?)
    exported_via = relationship("DBExport", back_populates="target_member", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<DBMember(fqn='{self.fully_qualified_name}', type='{self.member_type}')>"


class DBSignature(Base):
    """
    Stores signature variations for a member.
    """
    __tablename__ = 'signatures'
    
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey('members.id'), nullable=False)
    
    variant = Column(String, nullable=False) # e.g., 'full', 'default', 'no_types'
    signature_text = Column(Text, nullable=False)
    
    member = relationship("DBMember", back_populates="signatures")

    def __repr__(self):
        return f"<DBSignature(variant='{self.variant}')>"


class DBExport(Base):
    """
    Captures the 'export_records' from code analysis.
    
    Links an Exporting Module -> Target Member.
    This allows resolving "What does torch.nn export?" and "Where is Conv1d exposed?".
    """
    __tablename__ = 'exports'
    
    id = Column(Integer, primary_key=True)
    
    # Who is exporting? (e.g., module 'torch.nn')
    exporter_module_id = Column(Integer, ForeignKey('modules.id'), nullable=False)
    
    # What name is it exported as? (e.g., 'DataParallel')
    exported_name = Column(String, nullable=False) 
    
    # What is the underlying definition? (e.g., member 'torch.nn.parallel.DataParallel')
    # Nullable because sometimes exports point to external libraries we didn't analyze
    target_member_id = Column(Integer, ForeignKey('members.id'), nullable=True)
    
    # Metadata from export_records
    is_explicit = Column(Boolean, default=False) # True if in __all__
    is_reexport = Column(Boolean, default=False)
    is_wildcard = Column(Boolean, default=False)
    
    # Relationships
    exporter_module = relationship("DBModule", back_populates="exports")
    target_member = relationship("DBMember", back_populates="exported_via")

    # A module can export a specific name only once
    __table_args__ = (UniqueConstraint('exporter_module_id', 'exported_name', name='_mod_export_uc'),)

    def __repr__(self):
        return f"<DBExport({self.exported_name} from module {self.exporter_module_id})>"

class DBImport(Base):
    """
    Captures import statements from code analysis.
    
    Links an Importing Module -> Source Module/Entity.
    This allows resolving "What does this module depend on?" and "Where is X imported?"
    """
    __tablename__ = 'imports'
    
    id = Column(Integer, primary_key=True)
    
    # Who is importing? (e.g., module 'torch.nn.modules.conv')
    importer_module_id = Column(Integer, ForeignKey('modules.id'), nullable=False)
    
    # Import details
    line_number = Column(Integer, nullable=True)
    raw_module_specifier = Column(String, nullable=True)  # e.g., 'sklearn.model_selection'
    raw_imported_name = Column(String, nullable=True)     # e.g., 'ShuffleSplit'
    raw_alias = Column(String, nullable=True)             # e.g., 'np' for 'import numpy as np'
    
    # Import type flags
    is_relative = Column(Boolean, default=False)
    level = Column(Integer, default=0)  # Number of dots in relative import
    is_wildcard = Column(Boolean, default=False)
    
    # Resolved FQNs
    source_module_fqn = Column(String, nullable=True)     # e.g., 'numpy'
    imported_entity_fqn = Column(String, nullable=True)   # e.g., 'numpy.array'
    
    # What name is bound in the importer's namespace
    name_bound_in_importer = Column(String, nullable=True)     # e.g., 'np', 'array'
    name_bound_points_to_fqn = Column(String, nullable=True)   # What the bound name resolves to
    
    # Classification
    is_source_internal = Column(Boolean, default=False)  # True if importing from same codebase
    imported_is_member = Column(Boolean, nullable=True)
    imported_is_module = Column(Boolean, nullable=True)
    imported_is_package = Column(Boolean, nullable=True)
    
    # Relationships
    importer_module = relationship("DBModule", back_populates="imports")

    def __repr__(self):
        return f"<DBImport({self.name_bound_in_importer} from {self.source_module_fqn})>"
    

class DBInheritedMember(Base):
    """
    Represents an inherited member relationship.
    
    Captures the relationship between a class and a method it inherits,
    storing the derived API names for documentation linking.
    
    This table enables:
        - Finding inherited methods via the inheriting class's API name
        - Tracing the inheritance chain back to the original definition
        - Documentation linking for inherited members
    """
    __tablename__ = 'inherited_members'
    
    id = Column(Integer, primary_key=True)
    
    # The class that INHERITS this member
    inheriting_class_id = Column(Integer, ForeignKey('members.id'), nullable=False)
    
    # The ORIGINAL member being inherited (nullable if external)
    original_member_id = Column(Integer, ForeignKey('members.id'), nullable=True)
    
    # Basic identification
    member_name = Column(String, nullable=False)  # e.g., 'evals_result'
    member_type = Column(String, default='method')  # 'method', 'property', etc.
    
    # Source class information
    source_class_fqn = Column(String, nullable=True)  # e.g., 'xgboost.XGBModel'
    original_fqn = Column(String, nullable=True)  # e.g., 'xgboost.XGBModel.evals_result'
    
    # Derived API names (for querying via the inheriting class's path)
    # Primary: e.g., 'xgboost.XGBRFClassifier.evals_result'
    inherited_api_name = Column(String, index=True, nullable=True)
    # All possible API names for this inherited access path
    inherited_api_names = Column(JSON, nullable=True)
    
    # Original method's API names (for linking to original docs)
    original_api_name = Column(String, nullable=True)
    original_api_names = Column(JSON, nullable=True)
    
    # Signature for stop signal matching
    signature = Column(JSON, nullable=True)
    
    # Whether the source is external to the analyzed codebase
    is_external = Column(Boolean, default=False)
    
    # Documentation fields (for external inherited members)
    doc_format = Column(String, nullable=True)  # 'structured' or 'raw'
    doc_source_type = Column(String, nullable=True)  # 'web' or 'pdf'
    doc_source_path = Column(String, nullable=True)
    doc_raw_text = Column(Text, nullable=True)  # Full raw documentation text
    api_reference = Column(JSON, nullable=True)  # Structured JSON documentation
    doc_signature = Column(String, nullable=True)  # Extracted signature
    doc_description = Column(Text, nullable=True)  # Extracted description
    
    is_runtime_discovered = Column(Boolean, default=False)
    discovery_method = Column(String, nullable=True)  # 'static_inheritance', 'external_introspection', 'runtime_introspection'
    
    # Relationships
    inheriting_class = relationship(
        "DBMember", 
        foreign_keys=[inheriting_class_id],
        backref=backref('inherited_from', cascade="all, delete-orphan")
    )
    original_member = relationship(
        "DBMember", 
        foreign_keys=[original_member_id],
        backref=backref('inherited_by', cascade="all, delete-orphan")
    )
    
    # Unique constraint: a class can only inherit a given member once
    __table_args__ = (
        UniqueConstraint(
            'inheriting_class_id', 
            'member_name', 
            name='_class_inherited_member_uc'
        ),
    )
    
    def __repr__(self):
        return f"<DBInheritedMember({self.member_name} inherited by class_id={self.inheriting_class_id})>"
