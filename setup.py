"""
Setup configuration for the code analysis package.
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Runtime dependencies
# For Python 3.13, typed-ast is not needed. Astroid is for linters.
install_requires = [
    "typing-extensions>=4.0.0", # For type hints compatibility
    "requests>=2.25.0",         # For potential URL fetching
    "beautifulsoup4>=4.9.0",    # For HTML parsing if scraping
    "pyyaml>=5.4.0",            # For loading YAML config files
    "rich>=10.0.0",             # For enhanced CLI output
    "watchdog>=2.0.0",          # For file system watching in watch mode
    "networkx>=2.5",            # Core graph library
    "toml>=0.10.0",             # For parsing pyproject.toml (e.g., by DynamicAnalyzer for deps)
]

# Development dependencies
extras_require_dev = [
    "pytest>=7.0.0",
    "pytest-cov>=3.0.0",
    "coverage>=6.0.0",
    "black>=23.0.0",
    "isort>=5.10.0",
    "ruff>=0.1.0",   # Recommended: fast linter and formatter
    "mypy>=1.0.0",
    "astroid>=2.8.0", # If using pylint
    "pylint>=2.12.0", # If using pylint
]

setup(
    name="mapcodoc", 
    version="1.0.0", 
    author="Yeabsira Bekele",
    author_email="tadesse.yeabsira18@gmail.com",
    description="MapCoDoc: A tool for Code to API Documentation Traceability",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yab-tad/MapCoDoc",
    license="", #"MIT",
    
    packages=find_packages(
        where='.', 
        include=['code_analysis*', 'doc_processor*', 'mapcodoc_db*', 'cli*'],#, 'visualization*', 'docs_crawler*', 'linking*'],
        exclude=['tests*', '*.tests', '*.tests.*', 'test_analysis', 'pipeline_venv', 'test_repo_1', 'url_info']
    ),
    
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Documentation",
        "Topic :: Software Development :: Libraries :: Python Modules",
        # "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
    python_requires=">=3.9",
    
    install_requires=install_requires,
    extras_require={
        "dev": extras_require_dev,
    },
    
    entry_points={
        'console_scripts': [
            'mapcodoc = cli.main:main_cli', 
        ],
    },
)
