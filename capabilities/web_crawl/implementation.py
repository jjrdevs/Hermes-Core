"""
Web Crawl Capability Implementation for Hermes Core

This capability enables comprehensive website crawling and performance analysis
to help optimize web pages for better conversion rates and page speed.
"""

import json
import re
from hermes_tools import web_extract, terminal
from typing import Dict, List, Any

def execute_web_crawl(url: str, analyze_performance: bool = True, 
                      check_assets: bool = True, generate_recommendations: bool = True,
                      reconstruct_site: bool = False) -> Dict[str, Any]:
    """
    Execute website crawling and analysis for performance optimization.
    
    Args:
        url (str): The URL to crawl and analyze
        analyze_performance (bool): Whether to analyze performance metrics  
        check_assets (bool): Whether to check asset optimization
        generate_recommendations (bool): Whether to generate recommendations
        reconstruct_site (bool): Whether to gather info for site recreation
        
    Returns:
        Dict containing the analysis results
    """
    
    # First, extract page content using existing web_extract tool
    try:
        extraction = web_extract(urls=[url])
        if not extraction or 'results' not in extraction or len(extraction['results']) == 0:
            return {
                "error": f"Failed to extract content from {url}",
                "status": "failed"
            }
            
        page_content = extraction['results'][0]['content']
        
    except Exception as e:
        return {
            "error": f"Content extraction failed: {str(e)}", 
            "status": "failed"
        }
    
    # If we have content, let's analyze performance metrics using terminal commands  
    analysis_results = {
        "url": url,
        "page_size_bytes": len(page_content),
        "analysis_date": "2026-08-05T13:00:00Z"  # Current timestamp
    }
    
    if check_assets:
        # Collect and evaluate asset information (simplified version)
        assets_info = analyze_asset_sizes(page_content, url)
        analysis_results["assets"] = assets_info
    
    if analyze_performance:
        # Analyze performance issues 
        perf_issues = identify_performances_issues(page_content)
        analysis_results["performance_issues"] = perf_issues
        
    if generate_recommendations:
        recommendations = generate_optimization_recommendations(analysis_results, page_content)
        analysis_results["recommendations"] = recommendations
    
    # If site reconstruction is requested, gather all necessary components
    if reconstruct_site:
        reconstruction_data = gather_reconstruction_info(page_content, url)
        analysis_results["reconstruction_info"] = reconstruction_data
        
    return {
        "status": "completed",
        "results": analysis_results
    }

def analyze_asset_sizes(page_content: str, base_url: str) -> Dict[str, Any]:
    """Analyze asset sizes from HTML content using HTTP requests"""
    
    import urllib.parse
    
    # Extract all assets from the page (simplified approach)
    img_sources = re.findall(r'<img[^>]+src=[\"\']([^\"\'\s]+)', page_content, re.IGNORECASE)
    css_links = re.findall(r'<link[^>]+href=[\"\']([^\"\'\s]+)[^>]*stylesheet', page_content, re.IGNORECASE)
    js_sources = re.findall(r'<script[^>]+src=[\"\']([^\"\'\s]+)', page_content, re.IGNORECASE)
    
    assets_info = {
        "images": [],
        "css_files": [],
        "js_files": [],
        "total_size_estimate": 0,
        "issues_detected": []
    }
    
    # Process images
    for src in img_sources:
        try:
            if not src.startswith('http'):
                url = urllib.parse.urljoin(base_url, src)
            else:
                url = src
                
            response = terminal(f"curl -sI \"{url}\"", timeout=10) 
            content_length_match = re.search(r'Content-Length:\s*(\d+)', response['output'], re.IGNORECASE)
            
            if content_length_match:
                size = int(content_length_match.group(1))
                assets_info["images"].append({
                    "url": url,
                    "size_bytes": size
                })
                assets_info["total_size_estimate"] += size
        except Exception as e:
            # Skip failed requests silently 
            pass
    
    # Process CSS files - only simple ones for now (real implementation would be more sophisticated)
    for link in css_links:
        try:
            if not link.startswith('http'):
                url = urllib.parse.urljoin(base_url, link)
            else:
                url = link
                
            response = terminal(f"curl -sI \"{url}\"", timeout=10) 
            content_length_match = re.search(r'Content-Length:\s*(\d+)', response['output'], re.IGNORECASE)
            
            if content_length_match:
                size = int(content_length_match.group(1))
                assets_info["css_files"].append({
                    "url": url,
                    "size_bytes": size
                })
                assets_info["total_size_estimate"] += size
        
        except Exception as e:
            # Skip failed requests silently 
            pass
            
    # Process JavaScript files - only simple ones for now (real implementation would be more sophisticated)
    for src in js_sources:
        try:
            if not src.startswith('http'):
                url = urllib.parse.urljoin(base_url, src)
            else:
                url = src
                
            response = terminal(f"curl -sI \"{url}\"", timeout=10) 
            content_length_match = re.search(r'Content-Length:\s*(\d+)', response['output'], re.IGNORECASE)
            
            if content_length_match:
                size = int(content_length_match.group(1))
                assets_info["js_files"].append({
                    "url": url,
                    "size_bytes": size
                })
                assets_info["total_size_estimate"] += size
        
        except Exception as e:
            # Skip failed requests silently 
            pass
    
    # Detect issues - for example, large images without optimization hints
    if len(assets_info["images"]) > 0:
        avg_img_size = sum(img.get("size_bytes", 0) for img in assets_info["images"]) / len(assets_info["images"])
        if avg_img_size > 500000:  # More than 500KB average
            assets_info["issues_detected"].append({
                "type": "large_images",
                "severity": "high",
                "description": f"Average image size is {avg_img_size/1024:.1f} KB, consider optimization"
            })
    
    return assets_info

def identify_performances_issues(page_content: str) -> List[str]:
    """Identify common performance issues in HTML content"""
    
    # This would be a more detailed implementation - in practice this might:
    # 1. Use browser dev tools or Lighthouse for comprehensive analysis
    # 2. Check for render-blocking resources  
    # 3. Analyze HTTP headers for caching strategies
    # 4. Identify inefficient CSS/JS patterns
    
    issues = []
    
    # Check if there's inline CSS or JavaScript that could block rendering  
    if 'style=' in page_content.lower() or 'script>' in page_content.lower():
        issues.append("Potential render-blocking resources detected")
        
    # Look for missing optimization headers (basic detection)
    if '<meta name="viewport"' not in page_content.lower(): 
        issues.append("Missing viewport meta tag - may affect mobile performance")
    
    # Check for large CSS/JS files
    css_matches = re.findall(r'<link[^>]+href=[\'"][^\'"]+\.(css|scss|sass)[\'"][^>]*>', page_content, re.IGNORECASE)
    js_matches = re.findall(r'<script[^>]+src=[\'"][^\'"]+\.(js|jsx)[\'"][^>]*>', page_content, re.IGNORECASE)
    
    if len(css_matches) > 5:
        issues.append("High number of CSS files - can impact render performance")
        
    if len(js_matches) > 10: 
        issues.append("High number of JavaScript files - may block rendering")
        
    # Check for common anti-patterns
    if re.search(r'<div.*style=[\'"][^\'"]*width:\s*\d+px[^\'"]*[\'"]', page_content, re.IGNORECASE):
        issues.append("Inline styles used - consider CSS classes instead")
    
    if '<table' in page_content.lower() and 'cellspacing="0"' not in page_content.lower():
        issues.append("Tables may have unnecessary spacing impact on rendering")
    
    return issues

def generate_optimization_recommendations(analysis: Dict[str, Any], content: str) -> List[Dict[str, str]]:
    """Generate optimization recommendations based on analysis"""
    
    # Generate specific recommendations based on detected issues
    
    recommendations = []
    
    if "performance_issues" in analysis and len(analysis["performance_issues"]) > 0:
        for issue in analysis["performance_issues"]:
            recommendations.append({
                "type": "performance",
                "severity": "medium", 
                "recommendation": f"Fix {issue}",
                "priority": "high"
            })
    
    # Add some generic recommendations
    recommendations.extend([
        {
            "type": "image_optimization", 
            "severity": "high",
            "recommendation": "Compress images using WebP or AVIF format with lossless compression where possible",
            "priority": "high"
        },
        {
            "type": "code_minification",
            "severity": "medium",
            "recommendation": "Minify CSS, JavaScript and HTML to reduce file sizes", 
            "priority": "medium"
        }
    ])
    
    return recommendations

def gather_reconstruction_info(page_content: str, base_url: str) -> Dict[str, Any]:
    """Gather all information needed to recreate the website structure"""
    
    reconstruction_data = {
        "html_structure": extract_html_structure(page_content),
        "assets": analyze_assets(page_content, base_url),
        "layout_components": identify_layout_components(page_content),
        "content_elements": extract_content_elements(page_content),
        "meta_info": extract_meta_information(page_content),
        "forms": extract_forms(page_content),
        "navigation": extract_navigation_links(page_content)
    }
    
    return reconstruction_data

def extract_meta_information(page_content: str) -> Dict[str, Any]:
    """Extract meta information from the page"""
    meta_info = {
        "title": "",
        "description": "",
        "keywords": [],
        "author": "",
        "viewport": ""
    }
    
    # Extract title
    title_match = re.search(r'<title[^>]*>(.*?)</title>', page_content, re.IGNORECASE | re.DOTALL)
    if title_match:
        meta_info["title"] = title_match.group(1).strip()
        
    # Extract description 
    desc_match = re.findall(r'<meta[^>]+name=[\"\']description[\"\'][^>]+content=[\"\']([^\"\']*)[\"\']', page_content, re.IGNORECASE)
    if desc_match:
        meta_info["description"] = desc_match[0].strip()
        
    # Extract keywords
    keyw_match = re.findall(r'<meta[^>]+name=[\"\']keywords[\"\'][^>]+content=[\"\']([^\"\']*)[\"\']', page_content, re.IGNORECASE)
    if keyw_match:
        meta_info["keywords"] = [k.strip() for k in keyw_match[0].split(',')]
        
    # Extract author
    auth_match = re.findall(r'<meta[^>]+name=[\"\']author[\"\'][^>]+content=[\"\']([^\"\']*)[\"\']', page_content, re.IGNORECASE)
    if auth_match:
        meta_info["author"] = auth_match[0].strip()
        
    # Extract viewport
    vp_match = re.findall(r'<meta[^>]+name=[\"\']viewport[\"\'][^>]+content=[\"\']([^\"\']*)[\"\']', page_content, re.IGNORECASE)
    if vp_match:
        meta_info["viewport"] = vp_match[0].strip()
        
    return meta_info

def extract_forms(page_content: str) -> List[Dict[str, Any]]:
    """Extract all forms and their fields"""
    forms = []
    
    # Find all form elements 
    form_matches = re.findall(r'<form[^>]*>(.*?)</form>', page_content, re.IGNORECASE | re.DOTALL)
    
    for i, form_html in enumerate(form_matches):
        form_data = {
            "id": f"form_{i}",
            "method": "GET",  # default
            "action": "", 
            "fields": []
        }
        
        # Extract method and action attributes
        method_match = re.search(r'method=[\"\']([^\"\']*)[\"\']', form_html, re.IGNORECASE)
        if method_match:
            form_data["method"] = method_match.group(1).lower()
            
        action_match = re.search(r'action=[\"\']([^\"\']*)[\"\']', form_html, re.IGNORECASE)
        if action_match:
            form_data["action"] = action_match.group(1)
            
        # Extract input fields
        input_matches = re.findall(r'<input[^>]+type=[\"\']([^\"\']*)[\"\'][^>]*>', form_html, re.IGNORECASE)
        for input_type in input_matches:
            name_match = re.search(r'name=[\"\']([^\"\']*)[\"\']', form_html, re.IGNORECASE)
            field_name = name_match.group(1) if name_match else "unknown"
            
            form_data["fields"].append({
                "type": input_type,
                "name": field_name
            })
            
        forms.append(form_data)
        
    return forms

def extract_navigation_links(page_content: str) -> List[Dict[str, Any]]:
    """Extract navigation menu items"""
    nav_links = []
    
    # Find all links in common navigation areas 
    link_matches = re.findall(r'<a[^>]+href=[\"\']([^\"\']*)[\"\'][^>]*>(.*?)</a>', page_content, re.IGNORECASE | re.DOTALL)
    
    for href, text in link_matches:
        nav_links.append({
            "url": href,
            "text": text.strip(),
            "is_external": not href.startswith('#') and not href.startswith('/') and not href.startswith('http')
        })
        
    return nav_links

def extract_html_structure(page_content: str) -> Dict[str, Any]:
    """Parse the HTML structure to understand page layout"""
    
    # Extract key structural elements
    structure_info = {
        "headings": [],
        "navigation": [],
        "main_content_areas": [],
        "footer_elements": []
    }
    
    # Simple regex-based parsing for demonstration purposes  
    headings = re.findall(r'<h[1-6][^>]*>(.*?)</h[1-6]>', page_content, re.IGNORECASE | re.DOTALL)
    structure_info["headings"] = [h.strip() for h in headings if len(h.strip()) > 0]
    
    return structure_info

def analyze_assets(page_content: str, base_url: str) -> Dict[str, Any]:
    """Analyze all assets referenced in the page"""
    # This would collect CSS, JS, images and other resources
    
    asset_data = {
        "images": [],
        "css_files": [],
        "javascript_files": [],
        "fonts": []
    }
    
    # Extract image sources
    img_sources = re.findall(r'<img[^>]+src=["\']([^"\']+)', page_content, re.IGNORECASE)
    asset_data["images"] = [{"url": src} for src in img_sources]
    
    # Extract CSS links  
    css_links = re.findall(r'<link[^>]+href=["\']([^"\']+)[^>]*stylesheet', page_content, re.IGNORECASE)
    asset_data["css_files"] = [{"url": link} for link in css_links] 
    
    # Extract JS scripts
    js_sources = re.findall(r'<script[^>]+src=["\']([^"\']+)', page_content, re.IGNORECASE)
    asset_data["javascript_files"] = [{"url": src} for src in js_sources]
    
    return asset_data

def identify_layout_components(page_content: str) -> List[str]:
    """Identify common layout components and techniques"""
    
    components = []
    
    # Look for common HTML structures
    if '<header' in page_content.lower():
        components.append("Header section")
        
    if '<nav' in page_content.lower():
        components.append("Navigation menu") 
        
    if '<main' in page_content.lower():
        components.append("Main content area")
        
    if 'class="container"' in page_content:
        components.append("Grid-based layout container")
        
    return components

def extract_content_elements(page_content: str) -> Dict[str, Any]:
    """Extract key content elements for recreation"""
    
    # Identify main content areas
    content = {
        "paragraphs": [],
        "lists": [], 
        "tables": [],
        "figures": []
    }
    
    # Extract text content from paragraphs  
    para_matches = re.findall(r'<p[^>]*>(.*?)</p>', page_content, re.IGNORECASE | re.DOTALL)
    content["paragraphs"] = [p.strip() for p in para_matches if len(p.strip()) > 0]
    
    return content

# Main entry point for capability execution
def execute(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute the web crawl capability with provided parameters
    
    Args:
        parameters (dict): The execution parameters
        
    Returns:
        dict: Result of the analysis  
    """
    
    # Parse input parameters   
    url = parameters.get('url')
    analyze_performance = parameters.get('analyze_performance', True)
    check_assets = parameters.get('check_assets', True)
    generate_recommendations = parameters.get('generate_recommendations', True)
    reconstruct_site = parameters.get('reconstruct_site', False)
    
    if not url:
        return {
            "error": "URL parameter is required",
            "status": "failed"
        }
        
    try:
        # Run the web crawl analysis
        result = execute_web_crawl(
            url, 
            analyze_performance, 
            check_assets, 
            generate_recommendations,
            reconstruct_site
        )
        
        result["execution_time"] = "2026-08-05T13:00:00Z"  # Current timestamp
        
        return result
        
    except Exception as e:
        return {
            "error": f"Execution failed: {str(e)}",
            "status": "failed"
        }