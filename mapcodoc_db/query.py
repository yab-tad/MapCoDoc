"""
Query interface for the MapCoDoc database.

This module provides the QueryManager class, which encapsulates common
SQLAlchemy queries used by the documentation processor and CLI.
Supports comprehensive access to code analysis and documentation data.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from numpy import str_
from sqlalchemy.orm import Session, joinedload, strategies
from sqlalchemy import or_, and_, func, text

from mapcodoc_db.db_models import DBModule, DBMember, DBSignature, DBExport, DBImport, DBInheritedMember


# ============================================================================
# Data Classes for Query Results
# ============================================================================

@dataclass
class MemberDetails:
    """Comprehensive details for a code member."""
    id: int
    name: str
    fqn: str
    api_name: str
    api_names: List[str]
    api_name_sources: Dict[str, str]
    signatures: Dict[str, str]
    docstring: str
    type: str  # 'class', 'function', 'method', 'variable'
    source_code: str
    export_chain: List[Dict] = None
    line_start: int = None
    line_end: int = None
    access_modifier: str = "public"
    decorators: List[str] = None
    is_async: bool = False
    is_static: bool = False
    is_abstract: bool = False
    is_override: bool = False
    is_property: bool = False
    property_type: str = None
    is_chain_candidate: bool = False
    parameters: List[Dict] = None
    returns: Dict = None
    # Parent info
    parent_id: int = None
    parent_fqn: str = None


@dataclass
class MemberDocumentation:
    """Documentation details for a member."""
    member_id: int
    member_fqn: str
    member_api_name: str
    # Format indicator
    doc_format: str = None  # 'structured', 'raw', or None
    # Source info
    doc_source_type: str = None  # 'pdf', 'html', 'rst'
    doc_source_path: str = None
    doc_page_range: str = None
    doc_section_path: str = None
    doc_score: int = None
    # Full API reference
    api_reference_file: str = None
    api_reference: Dict = None  # For structured
    doc_raw_text: str = None    # For raw
    # Quick access fields
    doc_signature: str = None
    doc_description: str = None
    doc_examples: List[Dict] = None


@dataclass
class ModuleDetails:
    """Details for a module."""
    id: int
    name: str
    file_path: str
    is_package: bool
    has_all: bool
    all_exports: List[str]
    needs_dynamic_analysis: bool
    dynamic_analysis_attempted: bool
    dynamic_analysis_success: bool
    all_is_dynamic: bool = False
    statistics: Dict = None
    member_count: int = 0
    export_count: int = 0
    import_count: int = 0


@dataclass
class ImportDetails:
    """Details for an import statement."""
    id: int
    importer_module: str
    line_number: int
    source_module_fqn: str
    imported_entity_fqn: str
    name_bound: str  # What name is used in the importer
    alias: str = None
    is_relative: bool = False
    is_wildcard: bool = False
    is_internal: bool = False


@dataclass
class ExportDetails:
    """Details for an export relationship."""
    id: int
    exporter_module: str
    exported_name: str
    target_fqn: str = None
    target_type: str = None
    target_id: int = None # member ID for linking
    target_api_name: str = None
    signatures: Dict[str, str] = field(default_factory=dict) # for stop signals
    is_explicit: bool = False
    is_reexport: bool = False
    is_wildcard: bool = False


@dataclass
class InheritedMemberDetails:
    """Details for an inherited member relationship."""
    id: int
    member_name: str
    member_type: str
    # Inheriting class info
    inheriting_class_id: int
    inheriting_class_fqn: str
    inheriting_class_api_name: str
    # API names via inheriting class
    inherited_api_name: str
    inherited_api_names: List[str]
    # Original member info
    original_member_id: int = None
    source_class_fqn: str = None
    original_fqn: str = None
    original_api_name: str = None
    original_api_names: List[str] = field(default_factory=list)
    # Metadata
    signature: Dict = None
    is_external: bool = False
    # Documentation fields (for external members)
    doc_format: str = None
    doc_source_type: str = None
    doc_source_path: str = None
    doc_raw_text: str = None
    api_reference: Dict = None
    doc_signature: str = None
    doc_description: str = None


# ============================================================================
# Query Manager
# ============================================================================

class QueryManager:
    """
    High-level interface for querying code components and their metadata.
    Provides methods for both code analysis and documentation data.
    """
    
    def __init__(self, session: Session):
        self.session = session

    # ========================================================================
    # Module Queries
    # ========================================================================

    def get_all_modules(self) -> List[str]:
        """Retrieve a list of all module FQNs in the database."""
        modules = self.session.query(DBModule.name).all()
        return [m[0] for m in modules]

    def get_module_details(self, module_fqn: str) -> Optional[ModuleDetails]:
        """Get comprehensive details for a module."""
        mod = (
            self.session.query(DBModule)
            .filter(DBModule.name == module_fqn)
            .first()
        )
        if not mod:
            return None
        
        # Count related entities
        member_count = self.session.query(func.count(DBMember.id)).filter(
            DBMember.module_id == mod.id
        ).scalar()
        export_count = self.session.query(func.count(DBExport.id)).filter(
            DBExport.exporter_module_id == mod.id
        ).scalar()
        import_count = self.session.query(func.count(DBImport.id)).filter(
            DBImport.importer_module_id == mod.id
        ).scalar()
        
        return ModuleDetails(
            id=mod.id,
            name=mod.name,
            file_path=mod.file_path,
            is_package=mod.is_package,
            has_all=mod.has_all,
            all_exports=mod.all_exports or [],
            needs_dynamic_analysis=mod.needs_dynamic_analysis,
            dynamic_analysis_attempted=mod.dynamic_analysis_attempted,
            dynamic_analysis_success=mod.dynamic_analysis_success,
            all_is_dynamic=mod.all_is_dynamic,
            statistics=mod.module_statistics,
            member_count=member_count,
            export_count=export_count,
            import_count=import_count
        )

    def get_packages(self) -> List[str]:
        """Get all package modules (__init__.py files)."""
        packages = self.session.query(DBModule.name).filter(
            DBModule.is_package == True
        ).all()
        return [p[0] for p in packages]

    def get_modules_needing_dynamic_analysis(self) -> List[str]:
        """Get modules that need dynamic analysis."""
        modules = self.session.query(DBModule.name).filter(
            DBModule.needs_dynamic_analysis == True,
            DBModule.dynamic_analysis_attempted == False
        ).all()
        return [m[0] for m in modules]

    # ========================================================================
    # Member Queries - Code Analysis
    # ========================================================================

    def get_members_in_module(self, module_fqn: str) -> List[DBMember]:
        """Get all members DEFINED in a specific module."""
        return (
            self.session.query(DBMember)
            .join(DBModule)
            .filter(DBModule.name == module_fqn)
            .options(joinedload(DBMember.signatures))
            .all()
        )

    def get_member_details(self, fqn: str) -> Optional[MemberDetails]:
        """Get comprehensive details for a single member by FQN."""
        member = (
            self.session.query(DBMember)
            .filter(DBMember.fully_qualified_name == fqn)
            .options(
                joinedload(DBMember.signatures),
                joinedload(DBMember.parent)
            )
            .first()
        )
        
        if not member:
            return None
        
        api_names = member.all_api_names or []
        if not api_names:
            api_names = [member.fully_qualified_name]
        
        return MemberDetails(
            id=member.id,
            name=member.name,
            fqn=member.fully_qualified_name,
            api_names=api_names,
            api_name=member.primary_api_name,
            api_name_sources=member.api_name_sources or {},
            signatures={s.variant: s.signature_text for s in member.signatures},
            docstring=member.docstring or "",
            type=member.member_type,
            source_code=member.source_code or "",
            export_chain=member.best_export_chain or [],
            line_start=member.source_start_line,
            line_end=member.source_end_line,
            access_modifier=member.access_modifier,
            decorators=member.decorators or [],
            is_async=member.is_async,
            is_static=member.is_static,
            is_abstract=member.is_abstract,
            is_override=member.is_override,
            is_property=member.is_property,
            property_type=member.property_type,
            is_chain_candidate=member.is_chain_candidate,
            parameters=member.parameters,
            returns=member.returns,
            parent_id=member.parent_id,
            parent_fqn=member.parent.fully_qualified_name if member.parent else None
        )

    def get_member_by_api_name(self, api_name: str) -> Optional[MemberDetails]:
        """Find a member by its public API name."""
        member = (
            self.session.query(DBMember)
            .filter(DBMember.primary_api_name == api_name)
            .options(joinedload(DBMember.signatures))
            .first()
        )
        if member:
            return self.get_member_details(member.fully_qualified_name)
        return None
    
    def get_member_by_any_api_name(self, api_name: str) -> Optional[MemberDetails]:
        """
        Find member where api_name matches primary_api_name OR is in all_api_names.
        
        This is more flexible than get_member_by_api_name which only checks primary.
        Useful when doc filenames might match secondary API names.
        """
        # First try primary (fast index lookup)
        member = (
            self.session.query(DBMember)
            .filter(DBMember.primary_api_name == api_name)
            .options(joinedload(DBMember.signatures))
            .first()
        )
        if member:
            return self._member_to_details(member)
        
        # --- Fallback: search all_api_names JSON array contains the name ---
        # SQLite JSON: use json_each to search array elements
                
        # For SQLite, we need to check if api_name exists in the JSON array
        member = (
            self.session.query(DBMember)
            .filter(
                text("EXISTS (SELECT 1 FROM json_each(all_api_names) WHERE json_each.value = :api_name)")
            )
            .params(api_name=api_name)
            .options(joinedload(DBMember.signatures))
            .first()
        )
        
        if member:
            return self._member_to_details(member)
        
        return None

    def get_class_methods(self, class_fqn: str) -> List[MemberDetails]:
        """Get all methods for a specific class."""
        cls = self.session.query(DBMember).filter_by(
            fully_qualified_name=class_fqn, 
            member_type='class'
        ).first()
        
        if not cls:
            return []
        
        # Query children using parent_id relationship
        methods = (
            self.session.query(DBMember)
            .filter(DBMember.parent_id == cls.id)
            .options(joinedload(DBMember.signatures))
            .all()
        )
        
        return [self._member_to_details(m) for m in methods]

    def get_class_hierarchy(self, class_fqn: str) -> Dict[str, Any]:
        """
        Get full class hierarchy including nested classes and methods.
        
        Returns:
            {
                "class": MemberDetails,
                "methods": [MemberDetails, ...],
                "nested_classes": [{"class": MemberDetails, "methods": [...], ...}, ...]
            }
        """
        cls = self.session.query(DBMember).filter_by(
            fully_qualified_name=class_fqn,
            member_type='class'
        ).options(joinedload(DBMember.signatures)).first()
        
        if not cls:
            return None
        
        children = (
            self.session.query(DBMember)
            .filter(DBMember.parent_id == cls.id)
            .options(joinedload(DBMember.signatures))
            .all()
        )
        
        methods = [self._member_to_details(c) for c in children if c.member_type == 'method']
        nested_classes = [
            self.get_class_hierarchy(c.fully_qualified_name)
            for c in children if c.member_type == 'class'
        ]
        
        return {
            "class": self._member_to_details(cls),
            "methods": methods,
            "nested_classes": [nc for nc in nested_classes if nc]
        }

    def get_members_by_type(self, member_type: str, module_prefix: str = None) -> List[MemberDetails]:
        """
        Get all members of a specific type.
        
        Args:
            member_type: 'class', 'function', 'method', 'variable'
            module_prefix: Optional module FQN prefix to filter by
        """
        query = self.session.query(DBMember).filter(
            DBMember.member_type == member_type
        )
        
        if module_prefix:
            query = query.join(DBModule).filter(
                DBModule.name.like(f"{module_prefix}%")
            )
        
        members = query.options(joinedload(DBMember.signatures)).all()
        return [self._member_to_details(m) for m in members]

    def get_public_members(self, module_prefix: str = None) -> List[MemberDetails]:
        """Get all public members (those with API names)."""
        query = self.session.query(DBMember).filter(
            DBMember.primary_api_name.isnot(None)
        )
        
        if module_prefix:
            query = query.join(DBModule).filter(
                DBModule.name.like(f"{module_prefix}%")
            )
        
        members = query.options(joinedload(DBMember.signatures)).all()
        return [self._member_to_details(m) for m in members]

    def search_members(self, query_str: str, limit: int = 10) -> List[MemberDetails]:
        """Fuzzy search for members by name or FQN."""
        search = f"%{query_str}%"
        members = (
            self.session.query(DBMember)
            .filter(or_(
                DBMember.name.like(search),
                DBMember.fully_qualified_name.like(search),
                DBMember.primary_api_name.like(search)
            ))
            .options(joinedload(DBMember.signatures))
            .limit(limit)
            .all()
        )
        return [self._member_to_details(m) for m in members]

    
    def get_members_by_api_name_prefix(self, prefix: str) -> List[MemberDetails]:
        """
        Retrieve all members whose primary API name starts with the given prefix.
        
        Args:
            prefix: API name prefix (e.g., 'torch', 'numpy')
        
        Returns:
            List of MemberDetails for matching members.
        """
        members = (
            self.session.query(DBMember)
            .filter(DBMember.primary_api_name.like(f"{prefix}.%"))
            .options(joinedload(DBMember.signatures))
            .all()
        )
        return [self._member_to_details(m) for m in members]


    def get_all_public_members(self) -> List[MemberDetails]:
        """
        Retrieve all members with public access modifier.
        Fallback when library-prefixed query returns no results.
        
        Returns:
            List of MemberDetails for all public classes, functions, methods, and variables.
        """
        
        members = (
            self.session.query(DBMember)
            .filter(
                DBMember.access_modifier == "public",
                DBMember.member_type.in_(["class", "function", "method", "variable"])
            )
            .options(joinedload(DBMember.signatures))
            .all()
        )
        return [self._member_to_details(m) for m in members]
    
    
    # ========================================================================
    # Inherited Member Queries
    # ========================================================================

    def get_inherited_members_for_class(self, class_fqn: str) -> List[InheritedMemberDetails]:
        """
        Get all inherited members for a specific class.
        
        Args:
            class_fqn: FQN of the inheriting class
            
        Returns:
            List of InheritedMemberDetails for all inherited methods
        """
        cls = self.session.query(DBMember).filter_by(
            fully_qualified_name=class_fqn,
            member_type='class'
        ).first()
        
        if not cls:
            return []
        
        inherited = (
            self.session.query(DBInheritedMember)
            .filter(DBInheritedMember.inheriting_class_id == cls.id)
            .all()
        )
        
        return [self._inherited_to_details(im, cls) for im in inherited]


    def get_inherited_member_by_api_name(self, api_name: str) -> Optional[InheritedMemberDetails]:
        """
        Find an inherited member by its derived API name.
        
        This is the KEY method for documentation linking - when docs reference
        'xgboost.XGBRFClassifier.evals_result', this finds it even though
        evals_result is defined in XGBModel.
        
        Args:
            api_name: The full API path (e.g., 'xgboost.XGBRFClassifier.evals_result')
            
        Returns:
            InheritedMemberDetails if found, None otherwise
        """
        # First, try the indexed inherited_api_name column (fast)
        inherited = (
            self.session.query(DBInheritedMember)
            .filter(DBInheritedMember.inherited_api_name == api_name)
            .first()
        )
        
        if inherited:
            # Get the inheriting class for full details
            cls = self.session.query(DBMember).get(inherited.inheriting_class_id)
            return self._inherited_to_details(inherited, cls)
        
        # Fallback: search in inherited_api_names JSON array
        inherited = (
            self.session.query(DBInheritedMember)
            .filter(
                text("EXISTS (SELECT 1 FROM json_each(inherited_api_names) WHERE json_each.value = :api_name)")
            )
            .params(api_name=api_name)
            .first()
        )
        
        if inherited:
            cls = self.session.query(DBMember).get(inherited.inheriting_class_id)
            return self._inherited_to_details(inherited, cls)
        
        return None


    def get_original_member_for_inherited(self, inherited_api_name: str) -> Optional[MemberDetails]:
        """
        Given an inherited API name, find the ORIGINAL member definition.
        
        This is useful when you want the actual source code/docstring
        of an inherited method, not just the relationship.
        
        Args:
            inherited_api_name: e.g., 'xgboost.XGBRFClassifier.evals_result'
            
        Returns:
            MemberDetails of the original definition, or None
        """
        inherited = self.get_inherited_member_by_api_name(inherited_api_name)
        if not inherited or not inherited.original_member_id:
            return None
        
        original = self.session.query(DBMember).get(inherited.original_member_id)
        if original:
            return self._member_to_details(original)
        
        return None


    def find_member_by_any_path(self, api_name: str) -> Optional[Dict[str, Any]]:
        """
        COMPREHENSIVE member lookup that checks all possible paths.
        
        This is the recommended method for doc processing - it checks:
        1. Direct FQN match
        2. Primary API name match
        3. all_api_names JSON array
        4. Inherited member API names
        
        Args:
            api_name: Any API path to look up
            
        Returns:
            Dict with:
            - 'type': 'direct' | 'inherited'
            - 'member': MemberDetails (for direct) or InheritedMemberDetails
            - 'original_member': MemberDetails (for inherited, if available)
        """
        # Step 1: Try direct member lookup
        member = self.get_member_by_any_api_name(api_name)
        if member:
            return {
                'type': 'direct',
                'member': member,
                'original_member': None
            }
        
        # Step 2: Try inherited member lookup
        inherited = self.get_inherited_member_by_api_name(api_name)
        if inherited:
            original = None
            if inherited.original_member_id:
                original_db = self.session.query(DBMember).get(inherited.original_member_id)
                if original_db:
                    original = self._member_to_details(original_db)
            
            return {
                'type': 'inherited',
                'member': inherited,
                'original_member': original
            }
        
        return None


    def get_all_api_names_for_member(self, fqn: str) -> List[str]:
        """
        Get ALL possible API names for a member, including inherited paths.
        
        This includes:
        - The member's own API names
        - API names derived from classes that inherit this member
        
        Useful for building comprehensive documentation indexes.
        
        Args:
            fqn: Fully qualified name of the member
            
        Returns:
            Deduplicated list of all API names
        """
        all_names = set()
        
        # Get direct member API names
        member = self.session.query(DBMember).filter_by(
            fully_qualified_name=fqn
        ).first()
        
        if member:
            if member.primary_api_name:
                all_names.add(member.primary_api_name)
            if member.all_api_names:
                all_names.update(member.all_api_names)
            all_names.add(fqn)
            
            # Get inherited paths where this member is the original
            inherited_by = (
                self.session.query(DBInheritedMember)
                .filter(DBInheritedMember.original_member_id == member.id)
                .all()
            )
            
            for inherited in inherited_by:
                if inherited.inherited_api_name:
                    all_names.add(inherited.inherited_api_name)
                if inherited.inherited_api_names:
                    all_names.update(inherited.inherited_api_names)
        
        return sorted(all_names)


    def get_class_with_inherited_hierarchy(self, class_fqn: str) -> Optional[Dict[str, Any]]:
        """
        Get full class hierarchy including own methods AND inherited members.
        
        Extends get_class_hierarchy to include inherited_members.
        
        Returns:
            {
                "class": MemberDetails,
                "methods": [MemberDetails, ...],       # Directly defined
                "inherited_methods": [InheritedMemberDetails, ...],
                "nested_classes": [...]
            }
        """
        # Get base hierarchy
        hierarchy = self.get_class_hierarchy(class_fqn)
        if not hierarchy:
            return None
        
        # Add inherited members
        hierarchy["inherited_methods"] = self.get_inherited_members_for_class(class_fqn)
        
        return hierarchy


    # ========================================================================
    # Helper Methods for Inherited Members
    # ========================================================================

    def _inherited_to_details(
        self, 
        inherited: DBInheritedMember, 
        inheriting_class: DBMember = None
    ) -> InheritedMemberDetails:
        """Convert DBInheritedMember to InheritedMemberDetails dataclass."""
        # Get inheriting class if not provided
        if not inheriting_class:
            inheriting_class = self.session.query(DBMember).get(inherited.inheriting_class_id)
        
        return InheritedMemberDetails(
            id=inherited.id,
            member_name=inherited.member_name,
            member_type=inherited.member_type or 'method',
            inheriting_class_id=inherited.inheriting_class_id,
            inheriting_class_fqn=inheriting_class.fully_qualified_name if inheriting_class else "",
            inheriting_class_api_name=inheriting_class.primary_api_name if inheriting_class else "",
            inherited_api_name=inherited.inherited_api_name or "",
            inherited_api_names=inherited.inherited_api_names or [],
            original_member_id=inherited.original_member_id,
            source_class_fqn=inherited.source_class_fqn,
            original_fqn=inherited.original_fqn,
            original_api_name=inherited.original_api_name,
            original_api_names=inherited.original_api_names or [],
            signature=inherited.signature,
            is_external=inherited.is_external or False,
            doc_format=getattr(inherited, 'doc_format', None),
            doc_source_type=getattr(inherited, 'doc_source_type', None),
            doc_source_path=getattr(inherited, 'doc_source_path', None),
            doc_raw_text=getattr(inherited, 'doc_raw_text', None),
            api_reference=getattr(inherited, 'api_reference', None),
            doc_signature=getattr(inherited, 'doc_signature', None),
            doc_description=getattr(inherited, 'doc_description', None),
        )
    
    
    # ========================================================================
    # Member Queries - Documentation
    # ========================================================================

    def get_member_documentation(self, fqn_or_api_name: str) -> Optional[MemberDocumentation]:
        """Get documentation details for a member."""
        member = (
            self.session.query(DBMember)
            .filter(or_(
                DBMember.fully_qualified_name == fqn_or_api_name,
                DBMember.primary_api_name == fqn_or_api_name
            ))
            .first()
        )
        
        if not member:
            return None
        
        return MemberDocumentation(
            member_id=member.id,
            member_fqn=member.fully_qualified_name,
            member_api_name=member.primary_api_name,
            doc_source_type=member.doc_source_type,
            doc_source_path=member.doc_source_path,
            doc_page_range=member.doc_page_range,
            doc_section_path=member.doc_section_path,
            doc_score=member.doc_score,
            api_reference_file=member.api_reference_file,
            api_reference=member.api_reference,
            doc_signature=member.doc_signature,
            doc_description=member.doc_description,
            doc_examples=member.doc_examples
        )

    def get_members_with_documentation(self, module_prefix: str = None) -> List[MemberDocumentation]:
        """Get all members that have documentation."""
        query = self.session.query(DBMember).filter(
            DBMember.api_reference.isnot(None)
        )
        
        if module_prefix:
            query = query.join(DBModule).filter(
                DBModule.name.like(f"{module_prefix}%")
            )
        
        members = query.all()
        return [
            MemberDocumentation(
                member_id=m.id,
                member_fqn=m.fully_qualified_name,
                member_api_name=m.primary_api_name,
                doc_source_type=m.doc_source_type,
                doc_source_path=m.doc_source_path,
                doc_page_range=m.doc_page_range,
                doc_section_path=m.doc_section_path,
                doc_score=m.doc_score,
                api_reference_file=m.api_reference_file,
                api_reference=m.api_reference,
                doc_signature=m.doc_signature,
                doc_description=m.doc_description,
                doc_examples=m.doc_examples
            )
            for m in members
        ]

    def get_members_without_documentation(self, module_prefix: str = None) -> List[MemberDetails]:
        """Get public members that are missing documentation."""
        query = self.session.query(DBMember).filter(
            DBMember.primary_api_name.isnot(None),
            DBMember.api_reference.is_(None)
        )
        
        if module_prefix:
            query = query.join(DBModule).filter(DBModule.name.like(f"{module_prefix}%"))
        
        members = query.options(joinedload(DBMember.signatures)).all()
        return [self._member_to_details(m) for m in members]

    def get_documentation_coverage(self, module_prefix: str = None) -> Dict[str, Any]:
        """
        Get documentation coverage statistics.
        
        Returns:
            {
                "total_public_members": int,
                "documented_members": int,
                "coverage_percentage": float,
                "by_type": {
                    "class": {"total": int, "documented": int},
                    "function": {...},
                    "method": {...}
                }
            }
        """
        base_query = self.session.query(DBMember).filter(DBMember.primary_api_name.isnot(None))
        
        if module_prefix:
            base_query = base_query.join(DBModule).filter(
                DBModule.name.like(f"{module_prefix}%")
            )
        
        total = base_query.count()
        documented = base_query.filter(DBMember.api_reference.isnot(None)).count()
        
        # By type
        by_type = {}
        for member_type in ['class', 'function', 'method']:
            type_query = base_query.filter(DBMember.member_type == member_type)
            type_total = type_query.count()
            type_documented = type_query.filter(DBMember.api_reference.isnot(None)).count()
            by_type[member_type] = {
                "total": type_total,
                "documented": type_documented,
                "coverage": (type_documented / type_total * 100) if type_total > 0 else 0
            }
        
        return {
            "total_public_members": total,
            "documented_members": documented,
            "coverage_percentage": (documented / total * 100) if total > 0 else 0,
            "by_type": by_type
        }

    def get_member_documentation_by_format(self, fqn_or_api_name: str) -> Optional[Dict[str, Any]]:
        """
        Get documentation for a member, adapting response based on doc_format.
        
        Returns:
            Dict with:
            - 'format': 'structured' | 'raw' | None
            - 'content': The documentation content (JSON or raw text)
            - 'signature': Extracted signature
            - 'description': Description/summary
            - 'examples': Examples (only for structured)
            - 'source_type': 'web' | 'pdf'
            - 'source_path': Path to source file
        """
        member = (
            self.session.query(DBMember)
            .filter(or_(
                DBMember.fully_qualified_name == fqn_or_api_name,
                DBMember.primary_api_name == fqn_or_api_name
            ))
            .first()
        )
        
        if not member:
            return None
        
        doc_format = member.doc_format
        
        if doc_format == "structured":
            return {
                'format': 'structured',
                'content': member.api_reference,  # Full JSON
                'signature': member.doc_signature,
                'description': member.doc_description,
                'examples': member.doc_examples or [],
                'source_type': member.doc_source_type,
                'source_path': member.doc_source_path
            }
        elif doc_format == "raw":
            return {
                'format': 'raw',
                'content': member.doc_raw_text,  # Full raw text
                'signature': member.doc_signature,
                'description': member.doc_description,
                'examples': [],  # No examples for raw
                'source_type': member.doc_source_type,
                'source_path': member.doc_source_path
            }
        else:
            return {
                'format': None,
                'content': None,
                'signature': None,
                'description': None,
                'examples': [],
                'source_type': None,
                'source_path': None
            }


    def get_members_by_doc_format(self, doc_format: str, module_prefix: str = None) -> List[MemberDetails]:
        """
        Get all members with a specific documentation format.
        
        Args:
            doc_format: 'structured', 'raw', or 'none' (for undocumented)
            module_prefix: Optional module FQN prefix filter
            
        Returns:
            List of MemberDetails
        """
        if doc_format == 'none':
            query = self.session.query(DBMember).filter(DBMember.doc_format.is_(None))
        else:
            query = self.session.query(DBMember).filter(DBMember.doc_format == doc_format)
        
        if module_prefix:
            query = query.join(DBModule).filter(DBModule.name.like(f"{module_prefix}%"))
        
        members = query.options(joinedload(DBMember.signatures)).all()
        return [self._member_to_details(m) for m in members]


    def get_documentation_format_statistics(self) -> Dict[str, int]:
        """
        Get statistics on documentation formats in the database.
        
        Returns:
            Dict with counts: {'structured': N, 'raw': M, 'none': K}
        """
        structured = self.session.query(func.count(DBMember.id)).filter(
            DBMember.doc_format == 'structured'
        ).scalar()
        
        raw = self.session.query(func.count(DBMember.id)).filter(
            DBMember.doc_format == 'raw'
        ).scalar()
        
        none = self.session.query(func.count(DBMember.id)).filter(
            DBMember.doc_format.is_(None)
        ).scalar()
        
        return {
            'structured': structured,
            'raw': raw,
            'none': none
        }
    
    def get_inherited_member_documentation(self, api_name: str) -> Optional[Dict[str, Any]]:
        """
        Get documentation for an inherited member by API name.
        
        For internal inherited members, returns the original member's documentation.
        For external inherited members, returns documentation stored on the inherited record.
        
        Args:
            api_name: The inherited API path (e.g., 'xgboost.XGBClassifier.score')
            
        Returns:
            Dict with documentation fields, or None if not found
        """
        inherited = self.get_inherited_member_by_api_name(api_name)
        if not inherited:
            return None
        
        if inherited.original_member_id:
            # Internal - get from original member
            original = self.session.query(DBMember).get(inherited.original_member_id)
            if original:
                return {
                    'source': 'original_member',
                    'api_name': api_name,
                    'original_api_name': inherited.original_api_name,
                    'doc_format': original.doc_format,
                    'doc_raw_text': original.doc_raw_text,
                    'api_reference': original.api_reference,
                    'doc_signature': original.doc_signature,
                    'doc_description': original.doc_description,
                    'is_external': False
                }
        else:
            # External - get from inherited record itself
            return {
                'source': 'inherited_member',
                'api_name': api_name,
                'original_api_name': inherited.original_api_name,
                'doc_format': inherited.doc_format,
                'doc_raw_text': inherited.doc_raw_text,
                'api_reference': inherited.api_reference,
                'doc_signature': inherited.doc_signature,
                'doc_description': inherited.doc_description,
                'is_external': True
            }
        
        return None
    
    # ========================================================================
    # Export Queries
    # ========================================================================

    def get_public_peers(self, public_module_fqn: str) -> List[ExportDetails]:
        """
        Get all members EXPORTED by a public module.
        Primary query for Doc Processing 'Stop Signals'.
        """
        exporter = self.session.query(DBModule).filter_by(name=public_module_fqn).first()
        if not exporter:
            return []

        results = (
            self.session.query(DBExport)
            .filter(DBExport.exporter_module_id == exporter.id)
            .options(joinedload(DBExport.target_member).joinedload(DBMember.signatures))
            .all()
        )
        
        peers = []
        for exp in results:
            member = exp.target_member
            signatures = {}
            target_id = None
            target_api_name = None
            
            if member:
                signatures = {s.variant: s.signature_text for s in member.signatures}
                target_id = member.id
                target_api_name = member.primary_api_name
            
            peers.append(ExportDetails(
                id=exp.id,
                exporter_module=public_module_fqn,
                exported_name=exp.exported_name,
                target_fqn=member.fully_qualified_name if member else None,
                target_type=member.member_type if member else "unknown",
                target_id=target_id,
                target_api_name=target_api_name,
                signatures=signatures,
                is_explicit=exp.is_explicit,
                is_reexport=exp.is_reexport,
                is_wildcard=exp.is_wildcard
            ))
        return peers

    def get_exporting_modules_for_member(self, member_fqn: str) -> List[str]:
        """Get all modules that export a member, sorted by hierarchy (shortest first)."""
        member = self.session.query(DBMember).filter(
            DBMember.fully_qualified_name == member_fqn
        ).first()
        
        if not member or not member.api_name_sources:
            return []
        
        sources_dict = member.api_name_sources
        exporting_modules = set(sources_dict.values())
        return sorted(exporting_modules, key=lambda fqn: fqn.count('.'))

    def get_all_exports_for_member(self, member_fqn: str) -> List[ExportDetails]:
        """Get all export records that point to a specific member."""
        member = self.session.query(DBMember).filter_by(
            fully_qualified_name=member_fqn
        ).first()
        
        if not member:
            return []
        
        exports = (
            self.session.query(DBExport)
            .filter(DBExport.target_member_id == member.id)
            .options(joinedload(DBExport.exporter_module))
            .all()
        )
        
        return [
            ExportDetails(
                id=exp.id,
                exporter_module=exp.exporter_module.name,
                exported_name=exp.exported_name,
                target_fqn=member_fqn,
                target_type=member.member_type,
                is_explicit=exp.is_explicit,
                is_reexport=exp.is_reexport,
                is_wildcard=exp.is_wildcard
            )
            for exp in exports
        ]

    # ========================================================================
    # Import Queries
    # ========================================================================

    def get_module_imports(self, module_fqn: str) -> List[ImportDetails]:
        """Get all imports for a module."""
        mod = self.session.query(DBModule).filter_by(name=module_fqn).first()
        if not mod:
            return []
        
        imports = self.session.query(DBImport).filter(
            DBImport.importer_module_id == mod.id
        ).all()
        
        return [
            ImportDetails(
                id=imp.id,
                importer_module=module_fqn,
                line_number=imp.line_number,
                source_module_fqn=imp.source_module_fqn,
                imported_entity_fqn=imp.imported_entity_fqn,
                name_bound=imp.name_bound_in_importer,
                alias=imp.raw_alias,
                is_relative=imp.is_relative,
                is_wildcard=imp.is_wildcard,
                is_internal=imp.is_source_internal
            )
            for imp in imports
        ]

    def get_internal_dependencies(self, module_fqn: str) -> List[str]:
        """Get internal modules that a module depends on."""
        mod = self.session.query(DBModule).filter_by(name=module_fqn).first()
        if not mod:
            return []
        
        imports = self.session.query(DBImport.source_module_fqn).filter(
            DBImport.importer_module_id == mod.id,
            DBImport.is_source_internal == True
        ).distinct().all()
        
        return [i[0] for i in imports if i[0]]

    def get_external_dependencies(self, module_fqn: str) -> List[str]:
        """Get external modules that a module depends on."""
        mod = self.session.query(DBModule).filter_by(name=module_fqn).first()
        if not mod:
            return []
        
        imports = self.session.query(DBImport.source_module_fqn).filter(
            DBImport.importer_module_id == mod.id,
            DBImport.is_source_internal == False
        ).distinct().all()
        
        return [i[0] for i in imports if i[0]]

    def get_reverse_dependencies(self, module_fqn: str) -> List[str]:
        """Get modules that import from this module."""
        imports = (
            self.session.query(DBImport)
            .filter(DBImport.source_module_fqn == module_fqn)
            .options(joinedload(DBImport.importer_module))
            .all()
        )
        
        return list(set(imp.importer_module.name for imp in imports))

    # ========================================================================
    # Batch Queries for Doc Processing
    # ========================================================================

    def get_members_for_doc_processing(self, module_fqn_prefix: str) -> List[MemberDetails]:
        """
        Batch retrieve all members under a module prefix for documentation processing.
        """
        members = (
            self.session.query(DBMember)
            .join(DBModule)
            .filter(DBModule.name.like(f"{module_fqn_prefix}%"))
            .options(joinedload(DBMember.signatures))
            .all()
        )
        
        return [self._member_to_details(m) for m in members]

    def get_undocumented_public_api(self) -> List[Tuple[str, str, str]]:
        """
        Get list of undocumented public API members.
        
        Returns:
            List of (api_name, fqn, member_type) tuples
        """
        members = (
            self.session.query(
                DBMember.primary_api_name,
                DBMember.fully_qualified_name,
                DBMember.member_type
            )
            .filter(
                DBMember.primary_api_name.isnot(None),
                DBMember.api_reference.is_(None)
            )
            .all()
        )
        return [(m[0], m[1], m[2]) for m in members]

    # ========================================================================
    # Statistics
    # ========================================================================

    def get_database_statistics(self) -> Dict[str, Any]:
        """Get overall database statistics."""
        return {
            "modules": {
                "total": self.session.query(func.count(DBModule.id)).scalar(),
                "packages": self.session.query(func.count(DBModule.id)).filter(
                    DBModule.is_package == True
                ).scalar(),
                "with_all": self.session.query(func.count(DBModule.id)).filter(
                    DBModule.has_all == True
                ).scalar()
            },
            "members": {
                "total": self.session.query(func.count(DBMember.id)).scalar(),
                "classes": self.session.query(func.count(DBMember.id)).filter(
                    DBMember.member_type == 'class'
                ).scalar(),
                "functions": self.session.query(func.count(DBMember.id)).filter(
                    DBMember.member_type == 'function'
                ).scalar(),
                "methods": self.session.query(func.count(DBMember.id)).filter(
                    DBMember.member_type == 'method'
                ).scalar(),
                "with_api_name": self.session.query(func.count(DBMember.id)).filter(
                    DBMember.primary_api_name.isnot(None)
                ).scalar(),
                "with_documentation": self.session.query(func.count(DBMember.id)).filter(
                    DBMember.api_reference.isnot(None)
                ).scalar()
            },
            "inherited_members": self.session.query(func.count(DBInheritedMember.id)).scalar(),
            "exports": self.session.query(func.count(DBExport.id)).scalar(),
            "imports": self.session.query(func.count(DBImport.id)).scalar()
        }

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _member_to_details(self, member: DBMember) -> MemberDetails:
        """Convert DBMember to MemberDetails dataclass."""
        api_names = member.all_api_names or []
        if not api_names:
            api_names = [member.fully_qualified_name]
        
        return MemberDetails(
            id=member.id,
            name=member.name,
            fqn=member.fully_qualified_name,
            api_names=api_names,
            api_name=member.primary_api_name,
            api_name_sources=member.api_name_sources or {},
            signatures={s.variant: s.signature_text for s in member.signatures},
            docstring=member.docstring or "",
            type=member.member_type,
            source_code=member.source_code or "",
            export_chain=member.best_export_chain or [],
            line_start=member.source_start_line,
            line_end=member.source_end_line,
            access_modifier=member.access_modifier,
            decorators=member.decorators or [],
            is_async=member.is_async,
            is_static=member.is_static,
            is_abstract=member.is_abstract,
            is_override=member.is_override,
            is_property=member.is_property,
            property_type=member.property_type,
            is_chain_candidate=member.is_chain_candidate,
            parameters=member.parameters,
            returns=member.returns,
            parent_id=member.parent_id,
            parent_fqn=member.parent.fully_qualified_name if member.parent else None
        )
