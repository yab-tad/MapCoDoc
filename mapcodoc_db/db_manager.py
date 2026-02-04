"""
Database Manager for MapCoDoc.

Handles database connection, schema initialization, and the logic for 
ingesting hierarchical code analysis results into the relational schema.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from mapcodoc_db.db_models import Base, DBModule, DBMember, DBSignature, DBExport, DBImport, DBInheritedMember

logger = logging.getLogger(__name__)


class MapCoDocDB:
    """
    Manages SQLite database operations for MapCoDoc.
    """
    
    def __init__(self, db_path: str = "mapcodoc_output/mapcodoc.db", echo: bool = False):
        """
        Initialize DB connection.
        
        Args:
            db_path: Path to SQLite file.
            echo: If True, logs generated SQL.
        """
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=echo)
        self.SessionLocal = sessionmaker(bind=self.engine)
        
    def init_db(self, reset: bool = True):
        """
        Creates all tables defined in db_models.
        
        Args:
            reset: If True, deletes existing database file first.
        """
        if reset and Path(self.db_path).exists():
            logger.info(f"Resetting database: deleting {self.db_path}")
            # Close any connections first
            self.engine.dispose()
            os.remove(self.db_path)
            # Recreate engine after file deletion
            self.engine = create_engine(f"sqlite:///{self.db_path}", echo=False)
            self.SessionLocal = sessionmaker(bind=self.engine)
        
        Base.metadata.create_all(self.engine)
        logger.info("Database tables created/verified.")

    def get_session(self) -> Session:
        """Returns a new SQLAlchemy session."""
        return self.SessionLocal()

    def ingest_analysis_results(self, analysis_results: Dict[str, Any]):
        """
        Ingests the analysis JSON structure into the DB.
        
        Structure expected:
        {
            "repo_path/file.py": {
                "module_interface": {...},
                "components": { "fqn": {...}, ... },
                "export_records": [ ... ]
            },
            ...
        }
        """
        session = self.get_session()
        try:
            # Phase 1: Ingest Modules and Definitions (Members)
            # We need members to exist before we can link exports to them.
            
            # The analysis_results dict keys are file paths (or module FQNs depending on runner)
            # But the values definitely contain the module data.
            
            fqn_to_member_id = {} # Cache FQN -> DB ID for Phase 2
            
            for file_key, mod_data in analysis_results.items():
                # Skip metadata keys if any (like 'metrics' or 'errors')
                if file_key in ("metrics", "errors"):
                    continue
                    
                # 1. Create DBModule
                
                # Heuristic: grab the module FQN from the first component or export record
                module_fqn = self._extract_module_fqn(mod_data)
                if not module_fqn:
                    logger.warning(f"Could not determine module FQN for {file_key}. Skipping.")
                    continue
                
                db_mod = self._ingest_module_node(session, module_fqn, file_key, mod_data)
                session.flush() # Get ID
                
                # 2. Ingest Components (Members)
                components = mod_data.get("components", {})
                for comp_fqn, comp_data in components.items():
                    member_id = self._ingest_member_node(session, db_mod.id, comp_data)
                    if member_id:
                        fqn_to_member_id[comp_fqn] = member_id
            
            session.commit() # Commit Phase 1
            
            # Phase 1.5: Link parent-child relationships using parent_fqn
            # Now that ALL members are inserted, we can safely link them
            for file_key, mod_data in analysis_results.items():
                if file_key in ("metrics", "errors"):
                    continue
                
                components = mod_data.get("components", {})
                for comp_fqn, comp_data in components.items():
                    parent_fqn = comp_data.get("parent_fqn")
                    if parent_fqn and parent_fqn in fqn_to_member_id:
                        member_id = fqn_to_member_id.get(comp_fqn)
                        if member_id:
                            member = session.query(DBMember).get(member_id)
                            if member:
                                member.parent_id = fqn_to_member_id[parent_fqn]
            
            session.commit()  # Commit parent links
            
            # Phase 2: Ingest Export Records
            # Now that all modules and members exist, link them.
            for file_key, mod_data in analysis_results.items():
                if file_key in ("metrics", "errors"): continue
                
                module_fqn = self._extract_module_fqn(mod_data)
                if not module_fqn: continue
                
                # Find the exporter module ID
                exporter = session.query(DBModule).filter_by(name=module_fqn).first()
                if not exporter: continue
                
                exports = mod_data.get("export_records", [])
                for exp in exports:
                    self._ingest_export_record(session, exporter.id, exp, fqn_to_member_id)
            
            session.commit()
            logger.info("Analysis results ingested successfully.")
            
            # Phase 3: Ingest Import Records
            for file_key, mod_data in analysis_results.items():
                if file_key in ("metrics", "errors"):
                    continue
                
                module_fqn = self._extract_module_fqn(mod_data)
                if not module_fqn:
                    continue
                
                # Find the importer module ID
                importer = session.query(DBModule).filter_by(name=module_fqn).first()
                if not importer:
                    continue
                
                imports = mod_data.get("import_records", [])
                for imp in imports:
                    self._ingest_import_record(session, importer.id, imp)
                    
            session.commit()
            logger.info("Import records ingested successfully.")
            
            # Phase 4: Ingest Inherited Members
            # This creates queryable records for inherited methods enabling lookup by inheriting class's API path
            inherited_count = 0
            
            for file_key, mod_data in analysis_results.items():
                if file_key in ("metrics", "errors"):
                    continue
                
                components = mod_data.get("components", {})
                for comp_fqn, comp_data in components.items():
                    # Only process classes with inherited_methods
                    if comp_data.get("component_kind") != "class":
                        continue
                    if not comp_data.get("inherited_methods"):
                        continue
                    
                    # Get the inheriting class's DB ID
                    inheriting_class_id = fqn_to_member_id.get(comp_fqn)
                    if not inheriting_class_id:
                        # Try DB lookup
                        cls = session.query(DBMember).filter_by(
                            fully_qualified_name=comp_fqn
                        ).first()
                        if cls:
                            inheriting_class_id = cls.id
                    
                    if not inheriting_class_id:
                        logger.warning(f"Could not find DB ID for inheriting class: {comp_fqn}")
                        continue
                    
                    count = self._ingest_inherited_members(session, comp_data, inheriting_class_id, fqn_to_member_id)
                    inherited_count += count
            
            session.commit()
            if inherited_count > 0:
                logger.info(f"Ingested {inherited_count} inherited member records.")
            
        except Exception as e:
            session.rollback()
            logger.error(f"Error ingestion analysis results: {e}", exc_info=True)
            raise
        finally:
            session.close()

    def _extract_module_fqn(self, mod_data: Dict) -> Optional[str]:
        """Helper to find module FQN from data blob."""
        return mod_data.get("module_name")

    def _ingest_module_node(self, session: Session, fqn: str, file_path: str, mod_data: Dict) -> DBModule:
        """Create or get DBModule."""
        # Check if exists (idempotency)
        existing = session.query(DBModule).filter_by(name=fqn).first()
        if existing:
            return existing
            
        interface = mod_data.get("module_interface", {})
        statistics = mod_data.get("module_statistics", {})
        
        # needs_dynamic_analysis can be in module_interface or module_statistics
        needs_dynamic = (interface.get("needs_dynamic_analysis", False) or statistics.get("needs_dynamic_analysis", False))
        
        db_mod = DBModule(
            name=fqn,
            file_path=file_path,
            is_package=interface.get("is_init_file", False),
            has_all=interface.get("has_all", False),
            all_exports=interface.get("all_values", []),
            all_is_dynamic=interface.get("all_is_dynamic", False),
            needs_dynamic_analysis=needs_dynamic,
            dynamic_analysis_attempted=mod_data.get("dynamic_analysis_attempted", False),
            dynamic_analysis_success=mod_data.get("dynamic_analysis_success", False),
            module_statistics=mod_data.get("module_statistics")
        )
        session.add(db_mod)
        return db_mod

    def _ingest_member_node(self, session: Session, module_id: int, comp_data: Dict) -> Optional[int]:
        """Ingest a member and its signatures."""
        fqn = comp_data.get("fully_qualified_name")
        if not fqn: return None
        
        # Determine parent (for nested classes/methods)
        # Note: Since it's iterating a flat list of components, parents might not be inserted yet if order is random. However, SQLAlchemy can handle delayed FK resolution if inserted carefully,
        # OR we rely on the fact that we usually just need the module link.
        # For simplicity in this batch, link to Module. 
        # Linking Method->Class parent requires strictly ordered insertion or a second pass.
        # Given the flat 'components' dict, defer parent_id linking or try a simple lookup.
        
        # Check existence
        existing = session.query(DBMember).filter_by(fully_qualified_name=fqn).first()
        if existing:
            return existing.id

        # Determine access_modifier from name convention or explicit field
        name = comp_data.get("name", "")
        access_modifier = comp_data.get("access_modifier")
        if not access_modifier:
            if name.startswith("__") and not name.endswith("__"):
                access_modifier = "private"
            elif name.startswith("_"):
                access_modifier = "protected"
            else:
                access_modifier = "public"
        
        db_member = DBMember(
            module_id=module_id,
            name=name,
            fully_qualified_name=fqn,
            primary_api_name=comp_data.get("API_name"),
            all_api_names=comp_data.get("API_names", []),
            api_name_sources=comp_data.get("api_name_sources", {}),
            source_code=comp_data.get("body"),  
            member_type=comp_data.get("component_kind", "unknown"),
            docstring=comp_data.get("docstring"),
            # Source location
            source_start_line=comp_data.get("line_number"),
            source_end_line=comp_data.get("end_line"),
            # Access & visibility
            access_modifier=access_modifier,
            is_public=comp_data.get("is_public", True),
            # Function/Method characteristics
            is_async=comp_data.get("is_async", False),
            is_static=comp_data.get("is_static", False),
            is_abstract=comp_data.get("is_abstract", False),
            is_override=comp_data.get("is_override", False),
            is_property=comp_data.get("is_property", False),
            property_type=comp_data.get("property_type"),
            is_chain_candidate=comp_data.get("is_chain_candidate", False),
            # Parameters & returns
            parameters=comp_data.get("parameters"),
            returns=comp_data.get("returns"),
            # Export chain
            best_export_chain=comp_data.get("best_export_chain", [])
            # parent_id is left NULL for now unless we parse FQN to find parent
        )
        session.add(db_member)
        session.flush()
        
        # Signatures
        sigs = comp_data.get("signature", {})
        if isinstance(sigs, dict):
            for variant, text in sigs.items():
                session.add(DBSignature(
                    member_id=db_member.id,
                    variant=variant,
                    signature_text=str(text)
                ))
        
        return db_member.id

    def _ingest_inherited_members(
        self, 
        session: Session, 
        comp_data: Dict, 
        inheriting_class_id: int,
        fqn_map: Dict[str, int]
    ) -> int:
        """
        Ingest inherited members for a class.
        
        Creates DBInheritedMember records linking the inheriting class to
        inherited methods with their derived API names.
        
        Args:
            session: SQLAlchemy session
            comp_data: Component data dict containing 'inherited_methods'
            inheriting_class_id: DB ID of the inheriting class
            fqn_map: Map of FQN -> member DB ID for lookup
            
        Returns:
            Number of inherited members ingested
        """
        inherited_methods = comp_data.get("inherited_methods", {})
        if not inherited_methods:
            return 0
        
        count = 0
        for method_name, method_info in inherited_methods.items():
            if not isinstance(method_info, dict):
                continue
            
            # Find original member if it's internal
            original_fqn = method_info.get("original_fqn")
            original_member_id = fqn_map.get(original_fqn) if original_fqn else None
            
            # If not in fqn_map, try DB lookup (might be from another analysis batch)
            if not original_member_id and original_fqn:
                original = session.query(DBMember).filter_by(
                    fully_qualified_name=original_fqn
                ).first()
                if original:
                    original_member_id = original.id
            
            # Check for existing record to avoid duplicates
            existing = session.query(DBInheritedMember).filter_by(
                inheriting_class_id=inheriting_class_id,
                member_name=method_name
            ).first()
            
            if existing:
                # Update existing record with new API names if needed
                if method_info.get("inherited_api_name"):
                    existing.inherited_api_name = method_info["inherited_api_name"]
                if method_info.get("inherited_api_names"):
                    existing.inherited_api_names = method_info["inherited_api_names"]
                if method_info.get("original_api_name"):
                    existing.original_api_name = method_info["original_api_name"]
                if method_info.get("original_api_names"):
                    existing.original_api_names = method_info["original_api_names"]
                continue
            
            # Create new inherited member record
            db_inherited = DBInheritedMember(
                inheriting_class_id=inheriting_class_id,
                original_member_id=original_member_id,
                member_name=method_name,
                member_type=method_info.get("member_type", "method"),
                source_class_fqn=method_info.get("source_class_fqn"),
                original_fqn=original_fqn,
                inherited_api_name=method_info.get("inherited_api_name"),
                inherited_api_names=method_info.get("inherited_api_names", []),
                original_api_name=method_info.get("original_api_name"),
                original_api_names=method_info.get("original_api_names", []),
                signature=method_info.get("signature"),
                is_external=method_info.get("is_external", False),
                is_runtime_discovered=method_info.get("is_runtime_discovered", False),
                discovery_method=method_info.get("discovery_method"),
            )
            session.add(db_inherited)
            count += 1
        
        return count
    
    def _ingest_export_record(self, session: Session, exporter_id: int, exp_data: Dict, fqn_map: Dict[str, int]):
        """Ingest an export record linking module to member."""
        exported_name = exp_data.get("exported_name")
        if not exported_name:
            # Skip invalid export records without a name
            return
        
        target_fqn = exp_data.get("target_item_fqn")
        target_id = fqn_map.get(target_fqn)
        
        # If target is not in our map (e.g. it's from an external library we didn't analyze),
        # we can still record the export but target_member_id will be Null.
        # Or we try to find it in DB (maybe from another batch).
        if not target_id and target_fqn:
            target = session.query(DBMember).filter_by(fully_qualified_name=target_fqn).first()
            if target:
                target_id = target.id
        
        db_export = DBExport(
            exporter_module_id=exporter_id,
            exported_name=exported_name,
            target_member_id=target_id,
            is_explicit=exp_data.get("is_explicit", False),
            is_reexport=exp_data.get("is_reexport", False),
            is_wildcard=exp_data.get("is_wildcard_reexport", False)
        )
        session.add(db_export)
        
    def _ingest_import_record(self, session: Session, importer_id: int, imp_data: Dict):
        """Ingest an import record linking module to its imports."""
        db_import = DBImport(
            importer_module_id=importer_id,
            line_number=imp_data.get("line_number"),
            raw_module_specifier=imp_data.get("raw_module_specifier"),
            raw_imported_name=imp_data.get("raw_imported_name"),
            raw_alias=imp_data.get("raw_alias"),
            is_relative=imp_data.get("is_relative", False),
            level=imp_data.get("level", 0),
            is_wildcard=imp_data.get("is_wildcard", False),
            source_module_fqn=imp_data.get("source_module_fqn"),
            imported_entity_fqn=imp_data.get("imported_entity_fqn"),
            name_bound_in_importer=imp_data.get("name_bound_in_importer"),
            name_bound_points_to_fqn=imp_data.get("name_bound_points_to_fqn"),
            is_source_internal=imp_data.get("is_source_internal", False),
            imported_is_member=imp_data.get("imported_is_member"),
            imported_is_module=imp_data.get("imported_is_module"),
            imported_is_package=imp_data.get("imported_is_package")
        )
        session.add(db_import)
        
    def ingest_documentation_results(self, doc_results: Dict[str, Any], store_json_content: bool = True):
        """
        Updates existing members with documentation analysis results.
        
        Args:
            doc_results: Dict mapping member FQN/API name to doc data
                {
                    "torch.nn.L1Loss": {
                        "api_reference_file": "path/to/L1Loss.json",  # OR
                        "api_reference": { ... json content ... },
                        "doc_source_type": "html",
                        "doc_source_path": "https://pytorch.org/docs/...",
                        "doc_section_path": "torch.nn > Loss Functions > L1Loss",
                        "doc_score": 95
                    },
                    ...
                }
            store_json_content: If True and api_reference_file is provided, 
                            load and store the JSON content in api_reference column
        """
        session = self.get_session()
        updated_count = 0
        not_found_count = 0
        
        try:
            for member_identifier, doc_data in doc_results.items():
                if not member_identifier:
                    continue
                
                # Find the member by FQN or API name
                member = session.query(DBMember).filter_by(fully_qualified_name=member_identifier).first()
                if not member:
                    member = session.query(DBMember).filter_by(primary_api_name=member_identifier).first()
                if not member:
                    # Try searching in all_api_names (JSON array)
                    member = session.query(DBMember).filter(
                        DBMember.all_api_names.contains(member_identifier)
                    ).first()
                
                if not member:
                    logger.debug(f"Member not found for doc result: {member_identifier}")
                    not_found_count += 1
                    continue
                
                # Update source fields
                if "doc_source_type" in doc_data:
                    member.doc_source_type = doc_data["doc_source_type"]
                if "doc_source_path" in doc_data:
                    member.doc_source_path = doc_data["doc_source_path"]
                if "doc_page_range" in doc_data:
                    member.doc_page_range = doc_data["doc_page_range"]
                if "doc_section_path" in doc_data:
                    member.doc_section_path = doc_data["doc_section_path"]
                if "doc_score" in doc_data:
                    member.doc_score = doc_data["doc_score"]
                
                # Handle API reference JSON
                api_ref = doc_data.get("api_reference")
                api_ref_file = doc_data.get("api_reference_file")
                
                if api_ref_file:
                    member.api_reference_file = api_ref_file
                    # Optionally load and store the JSON content
                    if store_json_content and not api_ref:
                        try:
                            with open(api_ref_file, 'r', encoding='utf-8') as f:
                                api_ref = json.load(f)
                        except Exception as e:
                            logger.warning(f"Could not load API ref file {api_ref_file}: {e}")
                
                if api_ref:
                    member.api_reference = api_ref
                    # Extract quick-access fields
                    member.doc_signature = api_ref.get("module_member_signature")
                    desc = api_ref.get("module_member_description", {})
                    member.doc_description = desc.get("purpose") if isinstance(desc, dict) else str(desc)
                    member.doc_examples = api_ref.get("examples")
                
                updated_count += 1
            
            session.commit()
            logger.info(f"Documentation results ingested: {updated_count} updated, {not_found_count} not found.")
            
        except Exception as e:
            session.rollback()
            logger.error(f"Error ingesting documentation results: {e}", exc_info=True)
            raise
        finally:
            session.close()
        
        return {"updated": updated_count, "not_found": not_found_count}


    def ingest_documentation_from_directory(self, doc_dir: str, file_pattern: str = "*.json"):
        """
        Batch ingest documentation from a directory of JSON files.
        
        Assumes each JSON file is named after the member (e.g., "torch.nn.L1Loss.json")
        or contains a field identifying the member.
        
        Args:
            doc_dir: Directory containing the JSON documentation files
            file_pattern: Glob pattern for files to process
        """
        doc_path = Path(doc_dir)
        if not doc_path.exists():
            logger.error(f"Documentation directory not found: {doc_dir}")
            return {"updated": 0, "not_found": 0, "errors": 1}
        
        doc_results = {}
        
        for json_file in doc_path.glob(file_pattern):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                
                # Try to determine member identifier from file name or content
                member_id = json_file.stem  # e.g., "torch.nn.L1Loss" from "torch.nn.L1Loss.json"
                
                # Or if there's a field in the JSON identifying the member
                if "fully_qualified_name" in content:
                    member_id = content["fully_qualified_name"]
                elif "api_name" in content:
                    member_id = content["api_name"]
                
                doc_results[member_id] = {
                    "api_reference_file": str(json_file),
                    "api_reference": content
                }
                
            except Exception as e:
                logger.warning(f"Error reading {json_file}: {e}")
        
        return self.ingest_documentation_results(doc_results, store_json_content=False) 