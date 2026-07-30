"""
Improved CBS (Instacart) Invoice Parser
========================================
Complete OCR error handling and cleanup for Instacart receipts.
"""

import re

def clean_ocr_text(text):
    """Remove common OCR artifacts and garbage."""
    if not text:
        return ""
    
    # Remove leading OCR garbage
    text = re.sub(r'^[A-Z]{1,2}\s+(?=[A-Za-z])', '', text)  # Remove "EB ", "f ", etc.
    text = re.sub(r'^[a-z]\s+[;:,\-]\s+', '', text)  # Remove "f ; ", "mi fe I "
    text = re.sub(r'^(?:Pang|Te|nen|wen)\s+', '', text)  # Remove OCR garbage
    
    # Remove trailing OCR garbage
    text = re.sub(r'\s+(?:Pang|Te|nen|wen)\s*$', '', text)
    text = re.sub(r'\s+Final\s+.*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+\d+x\s*$', '', text)
    
    # Clean up multiple spaces
    text = re.sub(r'\s{2,}', ' ', text)
    
    return text.strip()


def is_valid_product_name(name):
    """Check if name looks like a real product."""
    if not name or len(name) < 3:
        return False
    
    # Must have mostly letters
    letters = len(re.findall(r'[A-Za-z]', name))
    if letters < len(name) * 0.4:  # At least 40% letters
        return False
    
    # Reject obvious non-products
    if re.search(r'\b(?:final|item|price|qty|amount|rate|total|subtotal|tax|fee|charge|items found|order totals)\b', name, re.I):
        return False
    
    return True


def match(pattern, text, group=1):
    """Helper function to extract regex matches."""
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(group).strip() if m else ""


def parse_cbs(t):
    """
    CBS (Canadian Bio Platforms) Instacart order receipt parser.
    Handles Instacart grocery delivery receipts with complete OCR cleanup.
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
    items_found = match(r"(\d+)\s+Items?\s+Found", t)
    if items_found:
        result["items_found"] = items_found
    
    # ── Extract Items by Category ──────────────────────────────────────────
    categories = re.findall(r"(DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|BEVERAGES|SNACKS|FROZEN|BAKERY|MEAT|PRODUCE)", t)
    if categories:
        result["categories"] = ", ".join(set(categories))
    
    # ── IMPROVED Item Extraction with Complete OCR Cleanup ─────────────────
    item_count = 0
    current_category = "Uncategorized"
    
    # Find the ITEMS FOUND section
    items_section_match = re.search(r"ITEMS FOUND.*?(?=ORDER TOTALS|REPLACEMENTS|$)", t, re.DOTALL | re.IGNORECASE)
    
    if items_section_match:
        items_text = items_section_match.group(0)
        raw_lines = items_text.split('\n')
        
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i].strip()
            
            # Skip empty lines
            if not line:
                i += 1
                continue
            
            # Check if this line is a category header
            category_match = re.match(r'^(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)$', line, re.IGNORECASE)
            if category_match:
                current_category = category_match.group(1).upper()
                i += 1
                continue
            
            # Skip "ITEMS FOUND" header
            if re.match(r'^ITEMS\s+FOUND', line, re.IGNORECASE):
                i += 1
                continue
            
            # Look for price pattern: "2x $44.75" or "$6.50" or "x $44.75"
            price_match = re.search(r'(\d+)\s*x\s*\$\s*([\d\.]+)', line)
            
            # Also check for standalone price line (e.g., "$6.50")
            if not price_match and re.match(r'^\$\s*[\d\.]+\s*$', line):
                # This is a standalone price line - look back for product name
                product_name = None
                for j in range(i - 1, max(0, i - 3), -1):
                    prev_line = raw_lines[j].strip()
                    
                    if not prev_line:
                        continue
                    
                    if re.match(r'^(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)$', prev_line, re.IGNORECASE):
                        continue
                    
                    if any(skip in prev_line.upper() for skip in ['ITEMS FOUND', 'REAL CANADIAN', 'ORDER TOTALS']):
                        continue
                    
                    cleaned_line = clean_ocr_text(prev_line)
                    
                    if is_valid_product_name(cleaned_line) and len(cleaned_line) >= 5:
                        product_name = cleaned_line
                        break
                
                if product_name:
                    # Extract price from current line
                    price_value = re.search(r'\$\s*([\d\.]+)', line)
                    if price_value:
                        final_price = price_value.group(1)
                        
                        # Check for duplicates
                        already_added = False
                        for idx in range(1, item_count + 1):
                            if result.get(f"item_{idx}_name") == product_name:
                                already_added = True
                                break
                        
                        if not already_added:
                            item_count += 1
                            result[f"item_{item_count}_category"] = current_category
                            result[f"item_{item_count}_name"] = product_name
                            result[f"item_{item_count}_quantity"] = "1"
                            result[f"item_{item_count}_unit_price"] = f"${final_price}"
                            result[f"item_{item_count}_final_price"] = f"${final_price}"
                            result[f"item_{item_count}_price_breakdown"] = f"1 × ${final_price} = ${final_price}"
                
                i += 1
                continue
            
            if price_match:
                quantity = price_match.group(1)
                unit_price = price_match.group(2)
                
                # Extract final price if present on same line
                final_price_match = re.search(r'\$\s*([\d\.]+)\s*$', line)
                if final_price_match:
                    final_price = final_price_match.group(1)
                else:
                    try:
                        final_price = f"{float(quantity) * float(unit_price):.2f}"
                    except:
                        final_price = unit_price
                
                # Look backwards for product name (up to 5 lines back)
                product_name = None
                for j in range(i - 1, max(0, i - 5), -1):
                    prev_line = raw_lines[j].strip()
                    
                    # Skip empty lines
                    if not prev_line:
                        continue
                    
                    # Skip category headers
                    if re.match(r'^(BEVERAGES|DAIRY & EGGS|HOUSEHOLD|PERSONAL CARE|SPECIAL REQUEST|FROZEN|BAKERY|MEAT|PRODUCE|SNACKS)$', prev_line, re.IGNORECASE):
                        continue
                    
                    # Skip lines with these keywords
                    if any(skip in prev_line.upper() for skip in ['ITEMS FOUND', 'REAL CANADIAN', 'ORDER TOTALS', 'REPLACEMENTS']):
                        continue
                    
                    # Skip lines that are just quantity/price patterns
                    if re.search(r'^\d+\s*x\s*\$', prev_line):
                        continue
                    
                    # Clean OCR garbage from the line
                    cleaned_line = clean_ocr_text(prev_line)
                    
                    # Check if this looks like a product name
                    if is_valid_product_name(cleaned_line) and len(cleaned_line) >= 5:
                        product_name = cleaned_line
                        break
                
                # If we found a product name, add it
                if product_name:
                    # Check for duplicates
                    already_added = False
                    for idx in range(1, item_count + 1):
                        if result.get(f"item_{idx}_name") == product_name:
                            already_added = True
                            break
                    
                    if not already_added:
                        item_count += 1
                        result[f"item_{item_count}_category"] = current_category
                        result[f"item_{item_count}_name"] = product_name
                        result[f"item_{item_count}_quantity"] = quantity
                        result[f"item_{item_count}_unit_price"] = f"${unit_price}"
                        result[f"item_{item_count}_final_price"] = f"${final_price}"
                        result[f"item_{item_count}_price_breakdown"] = f"{quantity} × ${unit_price} = ${final_price}"
            
            i += 1
    
    if item_count > 0:
        result["total_items_extracted"] = str(item_count)
    
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
    
    discount = match(r"\$(\d+)\s+off any store\s+-\$?([\d,\.]+)", t)
    if discount:
        result["discount"] = f"-${discount}"
    
    # Total - try multiple patterns
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
