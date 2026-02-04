#!/usr/bin/env python
"""
Run RQ1 Evaluation

Command-line interface for running RQ1 evaluations.

Usage:
    python -m tests.rq1_evaluation.run_evaluation \
        --db path/to/database.db \
        --crawl path/to/crawl_results.txt \
        --base-url https://docs.example.com/ \
        --sub-path api/reference \
        --prefix example \
        --name "Example Library" \
        --output ./reports
"""

import argparse
import logging
import sys
import json
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.rq1_evaluation import RQ1Evaluator, ReportGenerator, URLAPIExtractor


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


def main():
    parser = argparse.ArgumentParser(
        description="RQ1 Evaluation: API Path Resolution Accuracy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Evaluate XGBoost
  python run_evaluation.py \\
      --db ./mapcodoc_output/xgboost.db \\
      --crawl ./doc_artifacts/xgboost_urls.txt \\
      --base-url https://xgboost.readthedocs.io/en/stable/ \\
      --sub-path python/python_api \\
      --prefix xgboost \\
      --name "XGBoost"

  # Evaluate PyTorch
  python run_evaluation.py \\
      --db ./mapcodoc_output/pytorch.db \\
      --crawl ./doc_artifacts/pytorch_urls.txt \\
      --base-url https://pytorch.org/docs/stable/ \\
      --sub-path generated \\
      --prefix torch \\
      --name "PyTorch"
"""
    )
    
    parser.add_argument(
        '--db', required=True,
        help='Path to MapCoDoc SQLite database'
    )
    parser.add_argument(
        '--crawl', required=True,
        help='Path to crawled URLs file (txt or json)'
    )
    parser.add_argument(
        '--base-url', required=True,
        help='Base URL of documentation site'
    )
    parser.add_argument(
        '--sub-path', default='',
        help='API documentation sub-path (e.g., "api/reference")'
    )
    parser.add_argument(
        '--prefix', required=True,
        help='Package prefix for filtering DB members (e.g., "torch")'
    )
    parser.add_argument(
        '--name', default='Library',
        help='Human-readable library name for reports'
    )
    parser.add_argument(
        '--output', default='./evaluation_reports',
        help='Output directory for reports'
    )
    parser.add_argument(
        '--analyze-failures', action='store_true',
        help='Run detailed failure analysis (slower)'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)
    
    # Validate inputs
    if not Path(args.db).exists():
        logger.error(f"Database not found: {args.db}")
        sys.exit(1)
    
    if not Path(args.crawl).exists():
        logger.error(f"Crawl file not found: {args.crawl}")
        sys.exit(1)
    
    # Run evaluation
    logger.info(f"Starting RQ1 evaluation for {args.name}")
    
    try:
        with RQ1Evaluator(args.db) as evaluator:
            result = evaluator.evaluate(
                crawl_file=args.crawl,
                base_url=args.base_url,
                sub_path=args.sub_path,
                package_prefix=args.prefix,
                library_name=args.name
            )
        
        # Print summary
        print(result.summary())
        
        # Generate reports
        reporter = ReportGenerator(args.output, args.prefix)
        report_paths = reporter.generate_all(result, prefix="rq1")
        
        print("\n=== Generated Reports ===")
        for name, path in report_paths.items():
            print(f"  {name}: {path}")
        
        # Optional: Detailed failure analysis
        if args.analyze_failures:
            logger.info("Running failure analysis...")
            analysis = evaluator.analyze_failures(result)
            
            analysis_path = Path(args.output) / args.prefix / f"rq1_{args.name}_failure_analysis.json"
            with open(analysis_path, 'w') as f:
                json.dump(analysis, f, indent=2, default=str)
            print(f"  failure_analysis: {analysis_path}")
        
        # Exit with appropriate code
        if result.resolution_accuracy >= 0.9:
            logger.info("Evaluation passed (>90% accuracy)")
            sys.exit(0)
        elif result.resolution_accuracy >= 0.7:
            logger.warning("Evaluation marginal (70-90% accuracy)")
            sys.exit(0)
        else:
            logger.error("Evaluation failed (<70% accuracy)")
            sys.exit(1)
            
    except Exception as e:
        logger.exception(f"Evaluation failed: {e}")
        sys.exit(2)


if __name__ == '__main__':
    main()
    
# From project root
# python -m tests.rq1_evaluation.run_evaluation \
#     --db ./mapcodoc_output/xgboost/xgboost.db \
#     --crawl ./doc_processor/doc_artifacts/crawled_urls/xgboost_urls.txt \
#     --base-url "https://xgboost.readthedocs.io/en/stable/" \
#     --sub-path "python/python_api" \
#     --prefix "xgboost" \
#     --name "XGBoost" \
#     --output ./evaluation_reports \
#     --analyze-failures
