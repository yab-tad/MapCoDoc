"""
Report Generator for RQ1 Evaluation

Generates CSV, JSON, and LaTeX formatted reports from evaluation results.
"""

import json
import csv
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from .rq1_evaluator import RQ1EvaluationResult, MatchResult, MatchType


class ReportGenerator:
    """Generate evaluation reports in various formats."""
    
    def __init__(self, output_dir: str = "evaluation_reports", package_prefix: str = ""):
        self.output_dir = Path(output_dir) / package_prefix
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate_all(
        self, 
        result: RQ1EvaluationResult,
        prefix: str = ""
    ) -> Dict[str, Path]:
        """Generate all report formats."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{prefix}_{result.library_name}_{timestamp}" if prefix else f"{result.library_name}_{timestamp}"
        
        paths = {
            'summary': self.generate_summary(result, f"{name}_summary.txt"),
            'json': self.generate_json(result, f"{name}_full.json"),
            'csv_matches': self.generate_csv_matches(result, f"{name}_matches.csv"),
            'csv_unmatched': self.generate_csv_unmatched(result, f"{name}_unmatched.csv"),
            'latex': self.generate_latex_table(result, f"{name}_table.tex")
        }
        
        return paths
    
    def generate_summary(self, result: RQ1EvaluationResult, filename: str) -> Path:
        """Generate text summary."""
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(result.summary())
            
            # Add top unmatched
            f.write("\n--- Top 20 Unmatched Doc APIs ---\n")
            for api in result.unmatched_doc_apis[:20]:
                f.write(f"  {api}\n")
            
            if len(result.unmatched_doc_apis) > 20:
                f.write(f"  ... and {len(result.unmatched_doc_apis) - 20} more\n")
            
            f.write("\n--- Top 20 Unmatched DB Members ---\n")
            for api in result.unmatched_db_members[:20]:
                f.write(f"  {api}\n")
            
            if len(result.unmatched_db_members) > 20:
                f.write(f"  ... and {len(result.unmatched_db_members) - 20} more\n")
        
        return filepath
    
    def generate_json(self, result: RQ1EvaluationResult, filename: str) -> Path:
        """Generate full JSON report."""
        filepath = self.output_dir / filename
        
        data = {
            'metadata': {
                'library_name': result.library_name,
                'doc_source_url': result.doc_source_url,
                'db_path': result.db_path,
                'package_prefix': result.package_prefix,
                'generated_at': datetime.now().isoformat()
            },
            'counts': {
                'total_doc_apis': result.total_doc_apis,
                'total_db_members': result.total_db_members,
                'matched_count': result.matched_count
            },
            'match_breakdown': {
                'primary_matches': result.primary_matches,
                'candidate_matches': result.candidate_matches,
                'inherited_matches': result.inherited_matches,
                'fqn_matches': result.fqn_matches
            },
            'metrics': {
                'resolution_accuracy': result.resolution_accuracy,
                'coverage': result.coverage
            },
            'unmatched_doc_apis': result.unmatched_doc_apis,
            'unmatched_db_members': result.unmatched_db_members,
            'extraction_stats': result.extraction_stats,
            'match_details': [
                {
                    'doc_api_name': m.doc_api_name,
                    'matched': m.matched,
                    'match_type': m.match_type.value,
                    'db_api_name': m.db_api_name,
                    'db_fqn': m.db_fqn,
                    'member_type': m.member_type,
                    'is_inherited': m.is_inherited,
                    'notes': m.notes
                }
                for m in result.match_results
            ]
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        return filepath
    
    def generate_csv_matches(self, result: RQ1EvaluationResult, filename: str) -> Path:
        """Generate CSV of all match results."""
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'doc_api_name', 'matched', 'match_type', 'db_api_name',
                'db_fqn', 'member_type', 'is_inherited', 'notes'
            ])
            
            for m in result.match_results:
                writer.writerow([
                    m.doc_api_name,
                    m.matched,
                    m.match_type.value,
                    m.db_api_name or '',
                    m.db_fqn or '',
                    m.member_type or '',
                    m.is_inherited,
                    m.notes
                ])
        
        return filepath
    
    def generate_csv_unmatched(self, result: RQ1EvaluationResult, filename: str) -> Path:
        """Generate CSV of unmatched APIs."""
        filepath = self.output_dir / filename
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['source', 'api_name'])
            
            for api in result.unmatched_doc_apis:
                writer.writerow(['documentation', api])
            
            for api in result.unmatched_db_members:
                writer.writerow(['database', api])
        
        return filepath
    
    def generate_latex_table(self, result: RQ1EvaluationResult, filename: str) -> Path:
        """Generate LaTeX table for dissertation."""
        filepath = self.output_dir / filename
        
        latex = f"""% RQ1 Evaluation Results for {result.library_name}
% Generated: {datetime.now().isoformat()}

\\begin{{table}}[h]
\\centering
\\caption{{RQ1 Evaluation Results: {result.library_name}}}
\\label{{tab:rq1-{result.library_name.lower().replace(' ', '-')}}}
\\begin{{tabular}}{{lr}}
\\toprule
\\textbf{{Metric}} & \\textbf{{Value}} \\\\
\\midrule
Documentation APIs & {result.total_doc_apis:,} \\\\
Database Members & {result.total_db_members:,} \\\\
Matched & {result.matched_count:,} \\\\
\\midrule
Primary Matches & {result.primary_matches:,} \\\\
Candidate Matches & {result.candidate_matches:,} \\\\
Inherited Matches & {result.inherited_matches:,} \\\\
FQN Matches & {result.fqn_matches:,} \\\\
\\midrule
\\textbf{{Resolution Accuracy}} & \\textbf{{{result.resolution_accuracy:.2%}}} \\\\
\\textbf{{Coverage}} & \\textbf{{{result.coverage:.2%}}} \\\\
\\bottomrule
\\end{{tabular}}
\\end{{table}}
"""
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(latex)
        
        return filepath
    
    def generate_multi_library_table(
        self, 
        results: List[RQ1EvaluationResult],
        filename: str = "rq1_all_libraries.tex"
    ) -> Path:
        """Generate LaTeX table comparing multiple libraries."""
        filepath = self.output_dir / filename
        
        latex = f"""% RQ1 Evaluation Results - All Libraries
% Generated: {datetime.now().isoformat()}

\\begin{{table*}}[t]
\\centering
\\caption{{RQ1: API Path Resolution Accuracy Across Libraries}}
\\label{{tab:rq1-all}}
\\begin{{tabular}}{{lrrrrrrr}}
\\toprule
\\textbf{{Library}} & \\textbf{{Doc APIs}} & \\textbf{{DB Members}} & \\textbf{{Matched}} & \\textbf{{Primary}} & \\textbf{{Candidate}} & \\textbf{{Inherited}} & \\textbf{{Accuracy}} \\\\
\\midrule
"""
        
        for r in results:
            latex += f"{r.library_name} & {r.total_doc_apis:,} & {r.total_db_members:,} & {r.matched_count:,} & {r.primary_matches:,} & {r.candidate_matches:,} & {r.inherited_matches:,} & {r.resolution_accuracy:.1%} \\\\\n"
        
        # Add totals
        total_doc = sum(r.total_doc_apis for r in results)
        total_db = sum(r.total_db_members for r in results)
        total_matched = sum(r.matched_count for r in results)
        total_primary = sum(r.primary_matches for r in results)
        total_candidate = sum(r.candidate_matches for r in results)
        total_inherited = sum(r.inherited_matches for r in results)
        avg_accuracy = total_matched / total_doc if total_doc > 0 else 0
        
        latex += f"""\\midrule
\\textbf{{Total/Avg}} & {total_doc:,} & {total_db:,} & {total_matched:,} & {total_primary:,} & {total_candidate:,} & {total_inherited:,} & {avg_accuracy:.1%} \\\\
\\bottomrule
\\end{{tabular}}
\\end{{table*}}
"""
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(latex)
        
        return filepath