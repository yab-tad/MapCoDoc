"""
RQ1 Evaluator: API Path Resolution Accuracy

Evaluates MapCoDoc's API path resolution by comparing resolved API names
in the database against API names extracted from official documentation URLs.
"""

import logging
from dataclasses import dataclass, field
from typing import Set, Dict, List, Optional, Any, Tuple
from pathlib import Path
from enum import Enum

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add project root to path
import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mapcodoc_db.query import QueryManager, MemberDetails, InheritedMemberDetails
from .url_api_extractor import URLAPIExtractor, ExtractionConfig

logger = logging.getLogger(__name__)


class MatchType(Enum):
    """Types of API name matches."""
    PRIMARY = "primary"           # Matched via primary_api_name
    CANDIDATE = "candidate"       # Matched via all_api_names (secondary)
    INHERITED = "inherited"       # Matched via inherited_api_names
    FQN = "fqn"                   # Matched via fully_qualified_name
    NOT_FOUND = "not_found"       # No match found


@dataclass
class MatchResult:
    """Result of matching a single documentation API name."""
    doc_api_name: str
    matched: bool
    match_type: MatchType
    db_api_name: Optional[str] = None
    db_fqn: Optional[str] = None
    member_type: Optional[str] = None
    member_id: Optional[int] = None
    is_inherited: bool = False
    notes: str = ""


@dataclass
class RQ1EvaluationResult:
    """Complete results for RQ1 evaluation."""
    # Metadata
    library_name: str
    doc_source_url: str
    db_path: str
    package_prefix: str
    
    # Counts
    total_doc_apis: int
    total_db_members: int
    matched_count: int
    
    # Match breakdown by type
    primary_matches: int
    candidate_matches: int
    inherited_matches: int
    fqn_matches: int
    
    # Unmatched
    unmatched_doc_apis: List[str]
    unmatched_db_members: List[str]
    
    # Metrics
    resolution_accuracy: float  # matched / total_doc_apis
    coverage: float             # matched / total_db_members
    
    # Detailed results
    match_results: List[MatchResult] = field(default_factory=list)
    
    # Extraction stats
    extraction_stats: Dict = field(default_factory=dict)
    
    def summary(self) -> str:
        """Generate a text summary of results."""
        return f"""
=== RQ1 Evaluation Results: {self.library_name} ===

Documentation Source: {self.doc_source_url}
Database: {self.db_path}
Package Prefix: {self.package_prefix}

--- Counts ---
Documentation APIs: {self.total_doc_apis}
Database Members:   {self.total_db_members}
Matched:            {self.matched_count}

--- Match Breakdown ---
Primary API Name:   {self.primary_matches}
Candidate API Name: {self.candidate_matches}
Inherited API Name: {self.inherited_matches}
FQN Match:          {self.fqn_matches}

--- Metrics ---
Resolution Accuracy: {self.resolution_accuracy:.2%}
Coverage:            {self.coverage:.2%}

--- Unmatched ---
Doc APIs not in DB:  {len(self.unmatched_doc_apis)}
DB Members not in Docs: {len(self.unmatched_db_members)}
"""


class RQ1Evaluator:
    """
    Evaluator for RQ1: API Path Resolution Accuracy.
    
    Compares API names extracted from documentation URLs against
    API names resolved by MapCoDoc and stored in the database.
    
    Usage:
        evaluator = RQ1Evaluator('path/to/database.db')
        result = evaluator.evaluate(
            crawl_file='crawl_results.txt',
            base_url='https://docs.library.io/',
            sub_path='api/reference',
            package_prefix='library',
            library_name='MyLibrary'
        )
        print(result.summary())
    """
    
    def __init__(self, db_path: str):
        """
        Initialize evaluator with database path.
        
        Args:
            db_path: Path to the MapCoDoc SQLite database
        """
        self.db_path = db_path
        self.query_manager = QueryManager(db_path)
        
        # Create database session
        engine = create_engine(f"sqlite:///{db_path}")
        Session = sessionmaker(bind=engine)
        session = Session()
        
        self.query_manager = QueryManager(session)
        self._session = session  # Keep reference for cleanup
        
    def evaluate(
        self,
        crawl_file: str,
        base_url: str,
        sub_path: str = "",
        package_prefix: str = "",
        library_name: str = "Unknown",
        extraction_config: ExtractionConfig = None
    ) -> RQ1EvaluationResult:
        """
        Run complete RQ1 evaluation.
        
        Args:
            crawl_file: Path to file containing crawled documentation URLs
            base_url: Base URL of the documentation site
            sub_path: API documentation sub-path
            package_prefix: Package prefix to filter DB members (e.g., 'torch')
            library_name: Human-readable library name for reports
            extraction_config: Optional extraction configuration
            
        Returns:
            RQ1EvaluationResult with all metrics and details
        """
        logger.info(f"Starting RQ1 evaluation for {library_name}")
        
        # Step 1: Extract API names from documentation URLs
        extractor = URLAPIExtractor(base_url, sub_path, extraction_config)
        doc_api_names = extractor.extract_from_file(crawl_file)
        extraction_stats = extractor.get_stats()
        
        logger.info(f"Extracted {len(doc_api_names)} API names from {extraction_stats['total_urls']} URLs")
        
        # Step 2: Get all API names from database
        db_api_names, db_member_map = self._get_db_api_names(package_prefix)
        
        logger.info(f"Found {len(db_api_names)} API names in database for prefix '{package_prefix}'")
        
        # Step 3: Match documentation APIs against database
        match_results = []
        matched_doc_apis = set()
        matched_db_apis = set()
        
        match_counts = {
            MatchType.PRIMARY: 0,
            MatchType.CANDIDATE: 0,
            MatchType.INHERITED: 0,
            MatchType.FQN: 0,
            MatchType.NOT_FOUND: 0
        }
        
        for doc_api in doc_api_names:
            result = self._match_api_name(doc_api, db_member_map)
            match_results.append(result)
            
            match_counts[result.match_type] += 1
            
            if result.matched:
                matched_doc_apis.add(doc_api)
                if result.db_api_name:
                    matched_db_apis.add(result.db_api_name)
        
        # Step 4: Identify unmatched
        unmatched_doc_apis = sorted(doc_api_names - matched_doc_apis)
        # unmatched_db_members = sorted(db_api_names - matched_db_apis)
        
        # Step 5: Calculate metrics
        total_doc = len(doc_api_names)
        # total_db = len(db_api_names)
        matched = len(matched_doc_apis)
        
        resolution_accuracy = matched / total_doc if total_doc > 0 else 0.0
        # coverage = matched / total_db if total_db > 0 else 0.0
        
        return RQ1EvaluationResult(
            library_name=library_name,
            doc_source_url=base_url,
            db_path=self.db_path,
            package_prefix=package_prefix,
            total_doc_apis=total_doc,
            total_db_members=len(db_api_names),#total_db,
            matched_count=matched,
            primary_matches=match_counts[MatchType.PRIMARY],
            candidate_matches=match_counts[MatchType.CANDIDATE],
            inherited_matches=match_counts[MatchType.INHERITED],
            fqn_matches=match_counts[MatchType.FQN],
            unmatched_doc_apis=unmatched_doc_apis,
            unmatched_db_members=[],#unmatched_db_members,
            resolution_accuracy=resolution_accuracy,
            coverage=0.0,#coverage,
            match_results=match_results,
            extraction_stats=extraction_stats
        )
    
    def _get_db_api_names(self, package_prefix: str) -> Tuple[Set[str], Dict[str, Any]]:
        """
        Get all API names from the database for a given package prefix.
        
        Returns:
            Tuple of (set of all API names, mapping dict for lookups)
        """
        api_names = set()
        member_map = {
            'primary': {},      # primary_api_name -> MemberDetails
            'candidates': {},   # all other api_names -> MemberDetails
            'fqn': {},          # fqn -> MemberDetails
            'inherited': {}     # inherited_api_name -> InheritedMemberDetails
        }
        
        # Get direct members
        members = self.query_manager.get_members_by_api_name_prefix(package_prefix)
        
        for member in members:
            # Primary API name
            if member.api_name:
                api_names.add(member.api_name)
                member_map['primary'][member.api_name] = member
            
            # All API names (candidates)
            for api_name in member.api_names:
                api_names.add(api_name)
                if api_name != member.api_name:
                    member_map['candidates'][api_name] = member
            
            # FQN
            if member.fqn:
                member_map['fqn'][member.fqn] = member
                api_names.add(member.fqn)
        
        # Get inherited members
        inherited_members = self._get_inherited_members(package_prefix)
        for inherited in inherited_members:
            if inherited.inherited_api_name:
                api_names.add(inherited.inherited_api_name)
                member_map['inherited'][inherited.inherited_api_name] = inherited
            
            for api_name in inherited.inherited_api_names:
                api_names.add(api_name)
                member_map['inherited'][api_name] = inherited
        
        return api_names, member_map
    
    def _get_inherited_members(self, package_prefix: str) -> List[InheritedMemberDetails]:
        """Get inherited members for the package."""
        # This would require adding a method to QueryManager
        # For now, we'll use the comprehensive lookup
        inherited_list = []
        
        try:
            # Query inherited members directly
            from mapcodoc_db.db_models import DBInheritedMember, DBMember
            
            inherited_records = (
                self.query_manager.session.query(DBInheritedMember)
                .join(DBMember, DBInheritedMember.inheriting_class_id == DBMember.id)
                .filter(DBMember.primary_api_name.like(f"{package_prefix}.%"))
                .all()
            )
            
            for rec in inherited_records:
                cls = self.query_manager.session.query(DBMember).get(rec.inheriting_class_id)
                if cls:
                    details = self.query_manager._inherited_to_details(rec, cls)
                    inherited_list.append(details)
                    
        except Exception as e:
            logger.warning(f"Could not fetch inherited members: {e}")
        
        return inherited_list
    
    def _match_api_name(self, doc_api: str, db_member_map: Dict) -> MatchResult:
        """
        Match a documentation API name against the database.
        
        Checks in order:
        1. Primary API name (exact match)
        2. Candidate API names (all_api_names)
        3. Inherited API names
        4. FQN match
        """
        # Check primary
        if doc_api in db_member_map['primary']:
            member = db_member_map['primary'][doc_api]
            return MatchResult(
                doc_api_name=doc_api,
                matched=True,
                match_type=MatchType.PRIMARY,
                db_api_name=member.api_name,
                db_fqn=member.fqn,
                member_type=member.type,
                member_id=member.id,
                is_inherited=False,
                notes="Matched via primary API name"
            )
        
        # Check candidates
        if doc_api in db_member_map['candidates']:
            member = db_member_map['candidates'][doc_api]
            return MatchResult(
                doc_api_name=doc_api,
                matched=True,
                match_type=MatchType.CANDIDATE,
                db_api_name=member.api_name,
                db_fqn=member.fqn,
                member_type=member.type,
                member_id=member.id,
                is_inherited=False,
                notes=f"Matched via candidate API name (primary: {member.api_name})"
            )
        
        # Check inherited
        if doc_api in db_member_map['inherited']:
            inherited = db_member_map['inherited'][doc_api]
            return MatchResult(
                doc_api_name=doc_api,
                matched=True,
                match_type=MatchType.INHERITED,
                db_api_name=inherited.inherited_api_name,
                db_fqn=inherited.original_fqn,
                member_type='method',  # Inherited are typically methods
                member_id=inherited.original_member_id,
                is_inherited=True,
                notes=f"Matched via inherited API name (from {inherited.source_class_fqn})"
            )
        
        # Check FQN
        if doc_api in db_member_map['fqn']:
            member = db_member_map['fqn'][doc_api]
            return MatchResult(
                doc_api_name=doc_api,
                matched=True,
                match_type=MatchType.FQN,
                db_api_name=member.api_name,
                db_fqn=member.fqn,
                member_type=member.type,
                member_id=member.id,
                is_inherited=False,
                notes="Matched via FQN (doc uses implementation path)"
            )
        
        # No match found
        return MatchResult(
            doc_api_name=doc_api,
            matched=False,
            match_type=MatchType.NOT_FOUND,
            notes="No matching API name found in database"
        )
    
    def analyze_failures(
        self, 
        result: RQ1EvaluationResult,
        include_fuzzy: bool = True
    ) -> Dict[str, List[Dict]]:
        """
        Analyze unmatched cases to understand failure reasons.
        
        Args:
            result: Evaluation result to analyze
            include_fuzzy: Whether to include fuzzy match suggestions
            
        Returns:
            Dict with categorized failures and suggestions
        """
        analysis = {
            'doc_not_in_db': [],
            'db_not_in_doc': [],
            'possible_causes': {}
        }
        
        # Analyze unmatched doc APIs
        for doc_api in result.unmatched_doc_apis:
            entry = {
                'api_name': doc_api,
                'possible_cause': self._guess_failure_cause(doc_api),
                'fuzzy_matches': []
            }
            
            if include_fuzzy:
                entry['fuzzy_matches'] = self._find_fuzzy_matches(doc_api)
            
            analysis['doc_not_in_db'].append(entry)
        
        # Analyze unmatched DB members
        for db_api in result.unmatched_db_members:
            entry = {
                'api_name': db_api,
                'possible_cause': 'not_documented',
                'member_details': None
            }
            
            # Get member details
            member = self.query_manager.get_member_by_api_name(db_api)
            if member:
                entry['member_details'] = {
                    'type': member.type,
                    'fqn': member.fqn,
                    'has_docstring': bool(member.docstring)
                }
            
            analysis['db_not_in_doc'].append(entry)
        
        return analysis
    
    def _guess_failure_cause(self, api_name: str) -> str:
        """Guess why a documentation API wasn't found in DB."""
        # Check for common patterns
        if api_name.startswith('_'):
            return 'private_api'
        
        parts = api_name.split('.')
        if any(p.startswith('_') for p in parts):
            return 'internal_module'
        
        if any(p in ['test', 'tests', 'testing'] for p in parts):
            return 'test_code'
        
        if any(p in ['example', 'examples', 'demo'] for p in parts):
            return 'example_code'
        
        # Check if it might be dynamically generated
        if parts[-1].isupper() or parts[-1].startswith('TYPE_'):
            return 'type_alias_or_constant'
        
        return 'unknown'
    
    def _find_fuzzy_matches(self, api_name: str, max_results: int = 3) -> List[str]:
        """Find similar API names in the database."""
        try:
            from rapidfuzz import fuzz, process
            
            # Get all primary API names
            all_apis = list(self.query_manager.session.query(
                __import__('mapcodoc_db.db_models', fromlist=['DBMember']).DBMember.primary_api_name
            ).filter(
                __import__('mapcodoc_db.db_models', fromlist=['DBMember']).DBMember.primary_api_name.isnot(None)
            ).all())
            
            all_apis = [a[0] for a in all_apis if a[0]]
            
            if not all_apis:
                return []
            
            matches = process.extract(api_name, all_apis, scorer=fuzz.ratio, limit=max_results)
            return [m[0] for m in matches if m[1] > 60]  # Only >60% similarity
            
        except ImportError:
            logger.debug("rapidfuzz not available for fuzzy matching")
            return []
        except Exception as e:
            logger.debug(f"Fuzzy matching failed: {e}")
            return []
        
    def close(self):
        """Close the database session."""
        if hasattr(self, '_session') and self._session:
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()