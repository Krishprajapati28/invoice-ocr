"""
Complete CBS (Instacart) Invoice Parser
========================================
Handles the structured table format with ITEM 01, ITEM 02, etc.
Extracts complete product names, categories, quantities, and prices.
"""

import re

def clean_ocr_text(text):
    """Remove OCR artifacts while preserving product names."""
    if not text:
        return ""
    
    # Remove leading OCR garbage but preserve product names
    text = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Za-z])', '', text)  # Remove "EB ", "f ", etc.
    text = re.sub(r'^[a-z]\s+[;:,\-]\s+', '', text)  # Remove "f ; ", "mi fe I "
    text = re.sub(r'^(?:Pang|Te|nen|wen|=c\))\s+', '', text)  # Remove OCR garbage
    
    # Remove trailing OCR garbage
    text = re.sub(r'\s+(?:Pang|Te|nen|wen)\s*$', '', text)
    text = re.sub(r'\s+Final\s+.*$', '', text, flags=re.IGNORECASE)
    
    # Clean up multiple spaces
    text = re.sub(r'\s{2,}', ' ', text)
    
    return text.strip()


def extract_items_from_structured_format(t):
    """
    Extract items from structured format like:
    ITEM 01
    Original price: (10 x 355 ml) @ $8.70 - $8.70
    
    ITEM 02
    Replaced item (10 x 355 ml) @ $8.70 - $8.70
    
    ITEM 03
    Tetley Pure Green Tea (80 ct) (BEVERAGES) @ $12.30 - $12.30
    """
    items = []
    
    # Split by ITEM XX pattern
    item_blocks = re.split(r'ITEM\s+(\d+)', t, flags=re.IGNORECASE)
    
    # item_blocks[0] is before first ITEM, then alternates: [number, content, number, content, ...]
    for i in range(1, len(item_blocks), 2):
        if i + 1 >= len(item_blocks):
            break
        
        item_num = item_blocks[i].strip()
        item_content = item_blocks[i + 1].strip()
        
        if not item_content:
            continue
        
        # Parse the item content
        lines = [l.strip() for l in item_content.split('\n') if l.strip()]
        
        if not lines:
            continue
        
        # First line usually contains the product name and details
        first_line = lines[0]
        
        # Extract product name, category, quantity, and price
        product_name = ""
        category = ""
        quantity = ""
        unit_price = ""
        final_price = ""
        
        # Pattern: "Product Name (details) (CATEGORY) @ $price - $final_price"
        # or: "Product Name (details) @ $price - $final_price"
        # or: "Original price: (details) @ $price - $final_price"
        # or: "Replaced item (details) @ $price - $final_price"
        
        # Extract category (in parentheses, all caps)
        category_match = re.search(r'\(([A-Z\s&]+)\)', first_line)
        if category_match:
            potential_category = category_match.group(1).strip()
            if potential_category in ['BEVERAGES', 'DAIRY & EGGS', 'HOUSEHOLD', 'PERSONAL CARE', 'SPECIAL REQUEST', 'FROZEN', 'BAKERY', 'MEAT', 'PRODUCE', 'SNACKS']:
                category = potential_category
        
        # Extract prices: @ $X.XX - $Y.YY
        price_match = re.search(r'@\s*\$?([\d\.]+)\s*-\s*\$?([\d\.]+)', first_line)
        if price_match:
            unit_price = price_match.group(1)
            final_price = price_match.group(2)
        
        # Extract quantity: (XX x YYY ml) or (XX ct) etc.
        qty_match = re.search(r'\((\d+)\s*x\s*[\d\s\w]+\)', first_line)
        if qty_match:
            quantity = qty_match.group(1)
        else:
            # Try to find just a number in parentheses
            qty_match2 = re.search(r'\((\d+)\s*(?:ct|ml|oz|g|kg|L|l)\)', first_line)
            if qty_match2:
                quantity = qty_match2.group(1)
        
        # Extract product name (everything before the first parenthesis, cleaned)
        name_part = re.sub(r'\s*\(.*$', '', first_line).strip()
        name_part = clean_ocr_text(name_part)
        
        # Handle special cases
        if name_part.lower().startswith('original price'):
            name_part = "Original item"
        elif name_part.lower().startswith('replaced item'):
            name_part = "Replaced item"
        
        product_name = name_part
        
        # If we couldn't extract product name from first line, try to build it from all lines
        if not product_name or len(product_name) < 3:
            # Join all lines and extract product name
            full_content = ' '.join(lines)
            # Remove price info
            full_content = re.sub(r'@\s*\$[\d\.]+\s*-\s*\$[\d\.]+', '', full_content)
            # Remove category
            full_content = re.sub(r'\([A-Z\s&]+\)', '', full_content)
            # Remove quantity info
            full_content = re.sub(r'\(\d+\s*x\s*[\d\s\w]+\)', '', full_content)
            full_content = re.sub(r'\(\d+\s*(?:ct|ml|oz|g|kg|L|l)\)', '', full_content)
            
            product_name = clean_ocr_text(full_content)
        
        # Only add if we have a valid product name
        if product_name and len(product_name) >= 3:
            items.append({
                'number': item_num,
                'name': product_name,
                'category': category,
                'quantity': quantity,
                'unit_price': unit_price,
                'final_price': final_price,
                'raw_content': item_content
            })
    
    return items


def match(pattern, text, group=1):
    """Helper function to extract regex matches."""
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(group).strip() if m else ""


def parse_cbs_complete(t):
    """
    Complete CBS (Instacart) invoice parser.
    Handles structured table format with ITEM 01, ITEM 02, etc.
    """
    result = {}
    
    # ── Header Information ─────────────────────────────────────────────────
    if "Instacart" in t or "instacart" in t:
        result["platform"] = "Instacart"
    
    # Recipient email
    recipient_email = match(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", t)
    if recipient_email and "instacart" not in recipient_email.lower():
        result["recipient_email"] = recipient_email
    
    # Order date and time
    order_date = match(r"(?:Thu|Fri|Sat|Sun|Mon|Tue|Wed),?\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", t)
    if order_date:
        result["order_date"] = order_date
    
    order_time = match(r"at\s+(\d{1,2}:\d{2}\s+[AP]M)", t)
    if order_time:
        result["order_time"] = order_time
    
    # ── Store and Delivery Information ─────────────────────────────────────
    store = match(r"Your order from\s+(.+?)\s+was placed", t)
    if store:
        result["store_name"] = store.strip()
    
    placed_date = match(r"was placed on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", t)
    if placed_date:
        result["order_placed_date"] = placed_date
    
    delivery_date = match(r"delivered on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", t)
    if delivery_date:
        result["delivery_date"] = delivery_date
    
    delivery_time = match(r"delivered on.*?at\s+(\d{1,2}:\d{2}\s+[AP]M)", t)
    if delivery_time:
        result["delivery_time"] = delivery_time
    
    # Items found count
    items_found = match(r"(\d+)\s+Items?\s+(?:Found|Purchased)", t)
    if items_found:
        result["items_found"] = items_found
    
    # ── Extract Items from Structured Format ───────────────────────────────
    items = extract_items_from_structured_format(t)
    
    # Add items to result
    for idx, item in enumerate(items, 1):
        result[f"item_{idx:02d}_number"] = item['number']
        result[f"item_{idx:02d}_name"] = item['name']
        if item['category']:
            result[f"item_{idx:02d}_category"] = item['category']
        if item['quantity']:
            result[f"item_{idx:02d}_quantity"] = item['quantity']
        if item['unit_price']:
            result[f"item_{idx:02d}_unit_price"] = f"${item['unit_price']}"
        if item['final_price']:
            result[f"item_{idx:02d}_final_price"] = f"${item['final_price']}"
        
        # Create display format
        display = item['name']
        if item['category']:
            display += f" ({item['category']})"
        if item['quantity']:
            display += f" x{item['quantity']}"
        if item['unit_price']:
            display += f" @ ${item['unit_price']}"
        if item['final_price']:
            display += f" → ${item['final_price']}"
        
        result[f"item_{idx:02d}"] = display
    
    if items:
        result["total_items_extracted"] = str(len(items))
    
    # ── Order Totals ───────────────────────────────────────────────────────
    subtotal = match(r"Items Subtotal\s+\$?([\d,\.]+)", t)
    if subtotal:
        result["items_subtotal"] = f"${subtotal}"
    
    bag_fee = match(r"Checkout Bag Fee\s+\$?([\d,\.]+)", t)
    if bag_fee:
        result["checkout_bag_fee"] = f"${bag_fee}"
    
    bag_tax = match(r"Checkout Bag Fee Tax\s+\$?([\d,\.]+)", t)
    if bag_tax:
        result["checkout_bag_fee_tax"] = f"${bag_tax}"
    
    tip = match(r"Tip\s+\$?([\d,\.]+)", t)
    if tip:
        result["tip"] = f"${tip}"
    
    service_fee = match(r"Service Fee\s+\$?([\d,\.]+)", t)
    if service_fee:
        result["service_fee"] = f"${service_fee}"
    
    beverage_fee = match(r"Beverage Container Fee\s+\$?([\d,\.]+)", t)
    if beverage_fee:
        result["beverage_container_fee"] = f"${beverage_fee}"
    
    item_gst = match(r"Item GST\s+\$?([\d,\.]+)", t)
    if item_gst:
        result["item_gst"] = f"${item_gst}"
    
    service_gst = match(r"Service GST\s+\$?([\d,\.]+)", t)
    if service_gst:
        result["service_gst"] = f"${service_gst}"
    
    delivery_fee = match(r"Delivery Fee\s+\$?([\d,\.]+)", t)
    if delivery_fee:
        result["delivery_fee"] = f"${delivery_fee}"
    
    # Total
    total = (match(r"Total\s+CAD\s+\$?([\d,\.]+)", t) or
             match(r"Total CAD\s+\$?([\d,\.]+)", t) or
             match(r"Total\s+\$?([\d,\.]+)", t))
    if total:
        result["total_cad"] = f"${total}"
        result["order_total"] = f"${total}"
    
    # Currency
    currency = match(r"Total\s+(CAD|USD|EUR|GBP)", t)
    if currency:
        result["currency"] = currency
    else:
        if "CAD" in t:
            result["currency"] = "CAD"
    
    # ── Additional Information ─────────────────────────────────────────────
    if "Your Instacart order receipt" in t:
        result["document_type"] = "Instacart Order Receipt"
    
    if "Cbc Bio Platforms" in t or "CBS" in t or "Bio Platforms" in t:
        result["company"] = "CBS Bio Platforms"
    
    return {k: v for k, v in result.items() if v}
