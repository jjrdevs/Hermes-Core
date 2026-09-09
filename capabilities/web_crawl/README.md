# Web Crawl Capability

This capability enables comprehensive website crawling and analysis for performance improvements, with support for site reconstruction.

## Features

- Website content extraction using web_extract tool
- Performance analysis and optimization recommendations 
- Asset collection (images, CSS, JS files)
- Structure analysis for site recreation
- Layout component identification
- Content element extraction 

## Usage

The capability can be used in two modes:
1. Standard website analysis with performance optimization recommendations
2. Site reconstruction mode that gathers information needed to recreate the demo

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | (required) | The URL to crawl and analyze |
| analyze_performance | boolean | true | Whether to analyze performance metrics |
| check_assets | boolean | true | Whether to check image and asset optimization |
| generate_recommendations | boolean | true | Whether to generate optimization recommendations |
| reconstruct_site | boolean | false | Whether to gather information for site recreation |

## Integration

This capability integrates with existing Hermes Core tools:
- web_extract: For HTML content extraction
- terminal: For additional system commands (when needed)

## Implementation Details

The implementation is located in `implementation.py` and includes functions for:
- Website crawling and analysis  
- Asset size checking
- Performance issue identification
- Optimization recommendation generation
- Site reconstruction data gathering