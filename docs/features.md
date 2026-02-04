# Feature Flags System

The MapCoDoc feature flags system provides a way to toggle optional or experimental features at runtime without modifying code. This allows for different trade-offs between analysis speed, memory usage, and the exhaustiveness of the analysis.

## Available Flags

The following feature flags are currently available:

| Feature Flag | Description | Default | Stability |
|--------------|-------------|---------|-----------|
| `CHAIN_CANDIDATE_COLLECTION` | Enables proactive collection of **re-exported** module members as "chain candidates". Full export chain analysis is then focused on these candidates. | `True` | Stable |
| `API_BOUNDARY_DETECTION` | Enables heuristic scoring of modules (based on their statistics like `is_init_file`, export ratios) when `APIPathResolver` evaluates and scores export chains. Influences which chain is selected as the best public API path. | `True` | Stable |
| `ADVANCED_EXPORT_HEURISTICS` | Enables additional, potentially more complex or experimental, scoring rules within `APIPathResolver` when selecting the best export chain for a component. | `True` | Stable |
| `DYNAMIC_ALL_EVALUATION` | Controls whether `DynamicAnalyzer` executes module code in an isolated environment to resolve dynamic `__all__` lists and discover other runtime-only exports. | `False` | Beta |
| `GRAPH_ANALYSIS` | **(Performance Tuning)** Enables the entire in-memory graph building process (`GraphStore` and `Trackers`). If disabled (default), API path resolution relies solely on a faster, direct-lookup method and has no graph-based fallback. | `False` | Stable |
| `CALL_GRAPH_ANALYSIS` | **(Sub-feature of `GRAPH_ANALYSIS`)** Enables collection of function/method call relationships. Only has an effect if `GRAPH_ANALYSIS` is also enabled. | `False` | Experimental |
| `INCREMENTAL_WATCH_MODE` | Enables file system watching via `FileSystemWatcher` and triggers incremental re-analysis of changed modules and their dependents. Requires `GRAPH_ANALYSIS` to be enabled. | `False` | Experimental |


## Usage Patterns

### Checking if a Feature is Enabled

Use the `is_enabled()` function to check if a feature is enabled:

```python
from code_analysis.feature_flags import Feature, is_enabled

if is_enabled(Feature.GRAPH_ANALYSIS):
    # This block will only run if the graph feature is enabled
    self.graph_store.add_node(...)
```

### Enabling/Disabling Features Programmatically

Features can be enabled or disabled programmatically at the start of your application:

```python
from code_analysis.feature_flags import Feature, enable, disable

# Enable a feature
enable(Feature.DYNAMIC_ALL_EVALUATION)

# Disable a feature
disable(Feature.GRAPH_ANALYSIS)
```

### Configuration via Environment Variables

Features are most commonly controlled via environment variables for easy configuration of a run.

```bash
# Enable dynamic __all__ evaluation
export MAPCODOC_FEATURE_DYNAMIC_ALL_EVALUATION=1

# Enable the graph-based fallback
export MAPCODOC_FEATURE_GRAPH_ANALYSIS=1
```

The system supports the following values:
- **Enable:** `1`, `true`, `yes`, `on` (case-insensitive)
- **Disable:** `0`, `false`, `no`, `off` (case-insensitive)

### Configuration via CLI Flags

When using the CLI, feature flags can be enabled via command-line arguments:

```bash
python -m cli.main analyze ./repo \
    --enable-chain-candidates \
    --enable-api-boundaries \
    --enable-advanced-exports \
    --enable-dynamic-all \
    --enable-graph-analysis \
    --enable-call-graph
```

## Feature Interplay & Analysis Strategies

The MapCoDoc pipeline resolves the public API paths of code components using a tiered strategy. You can control this strategy with the feature flags to optimize for either speed or maximum accuracy.

### The Tiered Resolution Strategy

1.  **Tier 1: Fast Path (Graph-less)**
    *   This is the **default** analysis path used for every candidate. It works by performing a "Guided Virtual Graph Trace" directly on the collected analysis results without building or querying a large, in-memory graph.
    *   This method is extremely fast and memory-efficient and can correctly resolve the vast majority of common import and re-export patterns.

2.  **Tier 2 & 3: Graph-based Fallback (Optional)**
    *   This path is only activated if `GRAPH_ANALYSIS` is **enabled** and the Tier 1 Fast Path fails to find an export chain for a candidate.
    *   It uses the populated `GraphStore` to perform a more exhaustive search, first with a guided trace (Tier 2) and then with a full, blind search (Tier 3) if necessary.
    *   This provides a robust safety net for extremely complex or unusual code structures at the cost of higher memory usage and longer analysis time.

### How Flags Control the Tiers

*   **`GRAPH_ANALYSIS`**: This is the master switch for the fallback system.
    *   If `True`, the full in-memory graph is built, and the graph-based fallback (Tier 2/3) is available. Other features like `CALL_GRAPH_ANALYSIS` can be enabled.
    *   If `False` (default), the pipeline runs in a lightweight, "fast path only" mode. `GraphStore` and all `Trackers` are disabled, and memory usage is significantly lower. This is the recommended setting for most users.

*   **`CALL_GRAPH_ANALYSIS`**: This flag is a sub-feature of `GRAPH_ANALYSIS`. It has no effect if `GRAPH_ANALYSIS` is `False`. When both are enabled, it populates the graph with function and method call relationships, which can be used for downstream tasks like impact analysis but is not required for API path resolution.

*   **`CHAIN_CANDIDATE_COLLECTION`**, **`API_BOUNDARY_DETECTION`**, **`ADVANCED_EXPORT_HEURISTICS`**: These flags control the *quality* of the data that is fed into the resolution process, regardless of which tier is used. They are all enabled by default for best accuracy.

*   **`DYNAMIC_ALL_EVALUATION`**: Provides more accurate export data by executing modules to discover runtime `__all__`. Enable for libraries that dynamically construct their `__all__` lists.

### Recommended Configurations

*   **Default (Fast & Accurate for Most Cases):**
    *   `CHAIN_CANDIDATE_COLLECTION`, `API_BOUNDARY_DETECTION`, `ADVANCED_EXPORT_HEURISTICS` = `True` (default)
    *   `GRAPH_ANALYSIS` = `False` (default)
    *   This configuration works well for most libraries.

*   **For Libraries with Dynamic Exports:**
    *   Enable `DYNAMIC_ALL_EVALUATION` for libraries like NumPy that compute `__all__` at runtime.
    ```bash
    python -m cli.main analyze ./numpy --enable-dynamic-all
    ```

*   **For Maximum Robustness (Complex Re-export Patterns):**
    *   Enable `GRAPH_ANALYSIS` as a fallback for cases where the fast path fails.
    *   Enable `CALL_GRAPH_ANALYSIS` if you need call relationship data.
    ```bash
    python -m cli.main analyze ./complex-lib \
        --enable-graph-analysis \
        --enable-call-graph
    ```

---

## Adding New Feature Flags

To add a new feature flag:

1.  Add the flag to the `Feature` enum in `code_analysis/feature_flags.py`.
2.  Add its default state to the `_feature_state` dictionary in the same file.
3.  Document the new feature in this file.
4.  Use the feature flag in your code: `if is_enabled(Feature.MY_NEW_FEATURE): ...`
5.  Optionally add a CLI argument in `cli/main.py` for easy toggling.
