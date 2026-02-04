# MapCoDoc Event System

This document describes the event system used within the MapCoDoc pipeline, primarily managed by the `MapCoDocRegistry`. Events allow different components to communicate and react to changes in the system state without direct coupling.

## Overview

- **Publisher:** Components publish events using `registry.publish_event(event_name, payload, source_component)`.
- **Subscriber:** Components subscribe to events using `registry.subscribe_to_event(event_name, handler_method)`.
- **Payload:** Event data is wrapped in an `EventPayload` dictionary, which includes:
    - `timestamp`: Time the event was published (Unix timestamp float or ISO 8601 string).
    - `source_component`: Name of the component class that published the event.
    - `event_specific_data`: A dictionary containing the actual data relevant to the event.
- **Constants:** Event names are defined as constants in `code_analysis/events.py` to ensure consistency.

## Defined Events

Below is a list of currently defined events, their purpose, and the expected structure of their `event_specific_data` payload.

---

### Registry Events

-   **`REGISTRY_COMPONENT_REGISTERED` (`"registry_component_registered"`)**
    -   **Published by:** `MapCoDocRegistry`
    -   **Purpose:** Signals that a new component has been registered.
    -   **Payload (`event_specific_data`):**
        -   `component_name` (str): Name of the registered component.
        -   `component_type` (str): Type (class name) of the registered component.

-   **`REGISTRY_COMPONENT_UNREGISTERED` (`"registry_component_unregistered"`)**
    -   **Published by:** `MapCoDocRegistry`
    -   **Purpose:** Signals that a component has been unregistered.
    -   **Payload (`event_specific_data`):**
        -   `component_name` (str): Name of the unregistered component.

-   **`REGISTRY_STATE_SYNCED` (`"registry_state_synced"`)**
    -   **Published by:** `MapCoDocRegistry`
    -   **Purpose:** Signals that the registry's state has been synchronized.
    -   **Payload (`event_specific_data`):**
        -   `synced_components` (List[str]): List of component names successfully synced.
        -   `failed_components` (List[str]): List of component names that failed to sync.

-   **`DEPENDENCY_READY` (`"dependency_ready"`)**
    -   **Published by:** `MapCoDocRegistry` (or components notifying registry)
    -   **Purpose:** Signals that a specific component (dependency) is ready for use.
    -   **Payload (`event_specific_data`):**
        -   `dependency_name` (str): Name of the component that is now ready.

---

### Definition Registry Events

-   **`DEFINITION_REGISTERED` (`"definition_registered"`)**
    -   **Published by:** `DefinitionRegistry`
    -   **Purpose:** Signals a new code definition has been registered.
    -   **Payload (`event_specific_data`):**
        -   `definition` (Dict): Dictionary representation of `DefinitionInfo`.

-   **`DEFINITION_UPDATED` (`"definition_updated"`)**
    -   **Published by:** `DefinitionRegistry`
    -   **Purpose:** Signals an existing definition has been updated.
    -   **Payload (`event_specific_data`):**
        -   `definition` (Dict): Dictionary representation of the updated `DefinitionInfo`.

-   **`DEFINITION_REMOVED` (`"definition_removed"`)**
    -   **Published by:** `DefinitionRegistry`
    -   **Purpose:** Signals a definition has been removed.
    -   **Payload (`event_specific_data`):**
        -   `fully_qualified_name` (str): FQN of the removed definition.

---

### API Resolution & Analysis Cycle Events

-   **`CHAIN_CANDIDATES_UPDATED` (`"chain_candidates_updated"`)**
    -   **Published by:** `AnalyzerIntegration`
    -   **Purpose:** Signals the finalized set of component FQNs (primarily re-exported items) identified as candidates for export chain tracing. `APIPathResolver` subscribes to this to know which components to prioritize or handle with chain logic.
    -   **Payload (`event_specific_data`):**
        -   `candidates` (List[str]): List of component FQNs.
        -   `source` (str): Typically `AnalyzerIntegration.COMPONENT_NAME`.

-   **`API_PATH_RESOLVED` (`"api_path_resolved"`)**
    -   **Published by:** `APIPathResolver`
    -   **Purpose:** Signals that a single component's implementation FQN has been resolved to a public API FQN. Published for each successful resolution that results in a mapping.
    -   **Payload (`event_specific_data`):**
        -   `input_path` (str): The original component FQN that was resolved.
        -   `resolved_path` (str): The determined public API FQN.
        -   `resolution_source` (str): Method of resolution (e.g., "export_chain_selection", "local_definition_resolution").
        -   `is_correction` (bool): True if `resolved_path` is different from `input_path`.
        -   `chain_score` (Optional[float]): Score of the best chain, if resolved via chain.
        -   `best_chain_details` (Optional[List[Dict]]): The selected `ExportStep` list (as dicts), if resolved via chain.

-   **`API_MAP_UPDATED` (`"api_map_updated"`)**
    -   **Published by:** `APIPathResolver` (after individual updates) or `AnalyzerIntegration` (after a batch of candidate resolutions).
    -   **Purpose:** Signals that the API map (implementation FQN -> API FQN) within `APIPathResolver` has been updated. This can be due to new resolutions or invalidations.
    -   **Payload (`event_specific_data`):**
        -   `source` (str): Component that triggered the map update batch (e.g., `APIPathResolver`, `AnalyzerIntegration_candidate_batch`).
        -   `updated_entry_count` (Optional[int]): Number of entries added/updated in this batch.
        -   `invalidated_module` (Optional[str]): If update is due to invalidation, the module FQN that was invalidated.
        -   `details` (Optional[Dict]): Further details like specific keys updated/removed.

-   **`EXPORT_CHAINS_BUILT` (`"export_chains_built"`)**
    -   **Note:** This event might be less relevant if chains are built on-demand by `GraphTraversal` and the "best" is immediately selected by `APIPathResolver`. If `APIPathResolver` still pre-builds and stores selected best chains for all candidates in one go, this event could signal completion of that process.
    -   **Published by:** Potentially `APIPathResolver` if it has a distinct phase for this.
    -   **Purpose:** Signals that best export chains for candidates have been determined and stored (e.g., in `APIPathResolver.selected_export_chains`).
    -   **Payload (`event_specific_data`):**
        -   `chains_determined_count` (int): Number of candidates for which a best chain was selected.
        -   `candidates_processed_count` (int): Total number of candidates attempted.

---

### Analyzer Integration & File System Events

-   **`MODULE_ANALYSIS_UPDATED` (`"module_analysis_updated"`)**
    *   **Published by:** `AnalyzerIntegration` (after `analyze_file` completes for a module).
    *   **Purpose:** Signals that a module has been (re)analyzed, and its data (definitions, relationships, statistics, IR) is updated.
    *   **Payload (`event_specific_data`):**
        *   `module_path` (str): FQN of the module.
        *   `file_path` (str): Relative file path of the module.
        *   `success` (bool): True if analysis was successful.
        *   `ir_generated` (bool): True if IR was generated for this module.
        *   `ir_cache_hit` (bool): True if IR was loaded from disk cache.
        *   `component_count` (int): Number of components found in the module.
        *   `error_count` (int): Number of non-fatal errors during analysis of this module.
        *   `dynamic_analysis_attempted` (bool): True if dynamic analysis was attempted.
        *   `dynamic_analysis_success` (bool): True if dynamic analysis ran and yielded results.

-   **`MODULE_ANALYSIS_INVALIDATED` (`"module_analysis_invalidated"`)**
    *   **Published by:** `AnalyzerIntegration` (from `_perform_single_module_invalidation`).
    *   **Purpose:** Signals that data for a specific module (and potentially its dependents recursively) has been invalidated due to file changes or deletions. Consumers should discard cached data for this module.
    *   **Payload (`event_specific_data`):**
        *   `module_path` (str): FQN of the invalidated module.
        *   `file_path` (Optional[str]): Relative file path, if known (None if module was part of deleted package).
        *   `is_deleted` (bool): True if the module's file was deleted.

-   **`FILE_CREATED` / `FILE_MODIFIED` / `FILE_DELETED` (`"file_created"`, etc.)**
    *   **Published by:** `FileSystemWatcher`.
    *   **Consumed by:** `AnalyzerIntegration` to trigger re-analysis.
    *   **Payload (`event_specific_data`):**
        *   `file_path` (str): Absolute path to the file that changed.
        *   (Other potential watchdog event details if needed).

-   **`WATCHER_STARTED` / `WATCHER_STOPPED` / `WATCHER_ERROR`**
    *   **Published by:** `FileSystemWatcher`.
    *   **Purpose:** Lifecycle events for the watcher.

---

### Feature Flag Events

-   **`FEATURE_FLAG_CHANGED` (`"feature_flag_changed"`)**
    -   **Published by:** `feature_flags` module.
    -   **Purpose:** Signals a feature flag's state has changed.
    -   **Payload (`event_specific_data`):**
        -   `feature` (str): Name of the `Feature` enum member.
        -   `enabled` (bool): New state.
        -   `source` (str): How change was initiated (e.g., 'env', 'api').

---

### Database Events

-   **`DB_INGESTION_STARTED` (`"db_ingestion_started"`)**
    -   **Published by:** `MapCoDocDB`
    -   **Purpose:** Signals the start of analysis results ingestion into the database.
    -   **Payload (`event_specific_data`):**
        -   `db_path` (str): Path to the database file.
        -   `module_count` (int): Number of modules to ingest.

-   **`DB_INGESTION_COMPLETED` (`"db_ingestion_completed"`)**
    -   **Published by:** `MapCoDocDB`
    -   **Purpose:** Signals successful completion of database ingestion.
    -   **Payload (`event_specific_data`):**
        -   `db_path` (str): Path to the database file.
        -   `modules_ingested` (int): Number of modules ingested.
        -   `members_ingested` (int): Number of members ingested.
        -   `duration_seconds` (float): Time taken for ingestion.

-   **`DB_DOCUMENTATION_UPDATED` (`"db_documentation_updated"`)**
    -   **Published by:** `MapCoDocDB`
    -   **Purpose:** Signals that documentation has been added to member records.
    -   **Payload (`event_specific_data`):**
        -   `members_updated` (int): Number of members with documentation updated.

---

### Documentation Processing Events

-   **`DOC_CRAWL_STARTED` (`"doc_crawl_started"`)**
    -   **Published by:** `url_crawler` (via `save_urls_to_file`)
    -   **Purpose:** Signals the start of URL crawling for documentation.
    -   **Payload (`event_specific_data`):**
        -   `base_url` (str): The seed URL being crawled.
        -   `library_name` (str): Name of the library.
        -   `version` (str): Library version.

-   **`DOC_URL_DISCOVERED` (`"doc_url_discovered"`)**
    -   **Published by:** `url_crawler`
    -   **Purpose:** Signals discovery of a new documentation page under the target base URL.
    -   **Payload (`event_specific_data`):**
        -   `url` (str): Absolute URL of the discovered page.
        -   `depth` (int): Crawl depth (0 = seed page).

-   **`DOC_SCRAPE_COMPLETED` (`"doc_scrape_completed"`)**
    -   **Published by:** `doc_scraper` (via `scrape_doc`)
    -   **Purpose:** Signals completion of raw content download for documentation pages.
    -   **Payload (`event_specific_data`):**
        -   `library_name` (str): Name of the library.
        -   `pages_scraped` (int): Number of pages scraped.
        -   `layout_type` (str): Detected layout ('per_member', 'per_module', 'per_page').

-   **`DOC_MEMBER_EXTRACTED` (`"doc_member_extracted"`)**
    -   **Published by:** `DocProcessingRunner`
    -   **Purpose:** Signals that individual member documentation has been extracted from a combined page.
    -   **Payload (`event_specific_data`):**
        -   `api_name` (str): API name of the extracted member.
        -   `source_file` (str): Combined doc file it was extracted from.
        -   `match_type` (str): How the anchor was found ('lexical', 'semantic').
        -   `confidence` (float): Extraction confidence score.

-   **`DOC_LLM_EXTRACTION_STARTED` (`"doc_llm_extraction_started"`)**
    -   **Published by:** `ConcurrentDocExtractor`
    -   **Purpose:** Signals the start of LLM-based structured extraction.
    -   **Payload (`event_specific_data`):**
        -   `total_docs` (int): Number of documents to process.
        -   `max_concurrent` (int): Concurrency level.

-   **`DOC_LLM_EXTRACTION_PROGRESS` (`"doc_llm_extraction_progress"`)**
    -   **Published by:** `ConcurrentDocExtractor`
    -   **Purpose:** Progress update during LLM extraction.
    -   **Payload (`event_specific_data`):**
        -   `completed` (int): Number of documents completed.
        -   `total` (int): Total documents.
        -   `failed` (int): Number of failures.

-   **`DOC_STRUCTURED_EXTRACTED` (`"doc_structured_extracted"`)**
    -   **Published by:** `DocumentationExtractor` / `ConcurrentDocExtractor`
    -   **Purpose:** Signals that unstructured documentation has been converted into a structured schema via LLM.
    -   **Payload (`event_specific_data`):**
        -   `api_name` (str): API name of the member.
        -   `member_type` (str): Type ('class', 'function', 'method').
        -   `output_path` (str): Path to the structured JSON.
        -   `sections_extracted` (List[str]): List of extracted sections (e.g., 'parameters', 'returns', 'examples').

-   **`DOC_PROCESSING_COMPLETED` (`"doc_processing_completed"`)**
    -   **Published by:** `DocProcessingRunner`
    -   **Purpose:** Signals completion of the entire documentation processing pipeline.
    -   **Payload (`event_specific_data`):**
        -   `library_name` (str): Name of the library.
        -   `members_processed` (int): Number of members with documentation.
        -   `llm_used` (bool): Whether LLM extraction was used.
        -   `duration_seconds` (float): Total processing time.

---

### Trace Link Events

-   **`TRACE_LINK_CREATED` (`"trace_link_created"`)**
    -   **Published by:** `DocProcessingRunner` (during database update)
    -   **Purpose:** Signals creation of a trace link between a code component and documentation.
    -   **Payload (`event_specific_data`):**
        -   `member_fqn` (str): Fully-qualified name of the code member.
        -   `api_name` (str): Public API name.
        -   `doc_source` (str): Source of documentation ('pdf', 'html').
        -   `doc_path` (str): Path to the documentation file.
        -   `confidence` (float): Confidence score (0-1).

*This documentation should be kept up-to-date as new events are added or existing payloads change.*
