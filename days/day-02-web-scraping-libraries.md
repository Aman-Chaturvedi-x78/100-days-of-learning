---
date: 2026-07-21
day: 02
title: "Python Web Scraping Libraries: BeautifulSoup, Scrapy, and Selenium"
tags: [web-scraping, python, beautifulsoup, scrapy, selenium, http]
---

TL;DR
- **BeautifulSoup**: Simple, lightweight, great for parsing HTML. Best for small-to-medium scraping tasks.
- **Scrapy**: Full-featured framework for large-scale scraping. Handles pipelines, middleware, and concurrent requests.
- **Selenium**: Browser automation tool. Necessary for JavaScript-heavy sites that render content dynamically.

## Comparison Table

| Feature | BeautifulSoup | Scrapy | Selenium |
|---------|---------------|--------|----------|
| Learning Curve | Easy | Steep | Medium |
| Speed | Medium | Fast (async) | Slow (browser-based) |
| JS Rendering | ❌ No | ❌ No | ✅ Yes |
| Concurrency | Limited | ✅ Built-in | Limited |
| Best For | Quick scripts, parsing | Production crawlers | Dynamic content |
| Memory Usage | Low | Medium | High |

---

## 1. BeautifulSoup (Simple & Lightweight)

### What it does
- Parses HTML/XML documents
- Extracts data using CSS selectors or tag navigation
- Lightweight, easy to learn

### Installation
```bash
pip install beautifulsoup4 requests
```

### Basic Example: Scraping a Simple Page

```python
import requests
from bs4 import BeautifulSoup

# Fetch the page
url = "https://example.com"
response = requests.get(url)
soup = BeautifulSoup(response.content, "html.parser")

# Extract data using tag navigation
title = soup.title.string
print(f"Page title: {title}")

# Extract using CSS selectors
links = soup.select("a.nav-link")
for link in links:
    href = link.get("href")
    text = link.get_text()
    print(f"{text}: {href}")

# Extract using find_all
paragraphs = soup.find_all("p")
for p in paragraphs:
    print(p.get_text(strip=True))
```

### Real Example: Scrape Product Data

```python
import requests
from bs4 import BeautifulSoup
import time

def scrape_products(page_url):
    """Scrape product listings with polite delays."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    response = requests.get(page_url, headers=headers)
    soup = BeautifulSoup(response.content, "html.parser")
    
    products = []
    
    # Find all product containers
    product_items = soup.find_all("div", class_="product-item")
    
    for item in product_items:
        try:
            name = item.find("h2", class_="product-name").get_text(strip=True)
            price = item.find("span", class_="price").get_text(strip=True)
            url = item.find("a", class_="product-link").get("href")
            
            products.append({
                "name": name,
                "price": price,
                "url": url
            })
        except AttributeError:
            # Handle missing elements gracefully
            pass
    
    return products

# Scrape with rate limiting
products = scrape_products("https://example.com/products")
for product in products:
    print(f"{product['name']} - {product['price']}")
    time.sleep(2)  # Polite delay between requests
```

### Key Methods
```python
# Navigation
soup.find("tag")              # First match
soup.find_all("tag")          # All matches
soup.select("css.selector")   # CSS selector

# Extraction
element.get_text(strip=True)  # Extract text
element.get("attribute")      # Get attribute value
element["attribute"]          # Alternative attribute access

# Filtering
soup.find_all("div", class_="class-name")
soup.find_all("a", string="Link Text")
```

---

## 2. Scrapy (Production-Grade Framework)

### What it does
- Full-featured web scraping framework
- Built-in support for concurrency, pipelines, and middleware
- Handles robots.txt, retries, and caching automatically
- Best for large-scale scraping projects

### Installation
```bash
pip install scrapy
```

### Project Structure
```bash
scrapy startproject myproject
cd myproject
scrapy genspider quotes quotes.toscrape.com
```

### Basic Spider Example

```python
# myproject/spiders/quotes_spider.py
import scrapy

class QuotesSpider(scrapy.Spider):
    name = "quotes"
    allowed_domains = ["quotes.toscrape.com"]
    start_urls = ["https://quotes.toscrape.com"]
    
    def parse(self, response):
        """Main parsing method."""
        
        # Extract quotes
        for quote in response.css("div.quote"):
            yield {
                "text": quote.css("span.text::text").get(),
                "author": quote.css("small.author::text").get(),
                "tags": quote.css("a.tag::text").getall()
            }
        
        # Follow pagination
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, self.parse)
```

### Running the Spider
```bash
# Output to JSON
scrapy crawl quotes -o quotes.json

# Output to CSV
scrapy crawl quotes -o quotes.csv

# Log output (default to console)
scrapy crawl quotes
```

### Advanced: Custom Pipeline for Data Processing

```python
# myproject/pipelines.py
import sqlite3
from itemadapter import ItemAdapter

class SQLitePipeline:
    """Store scraped items in SQLite database."""
    
    def open_spider(self, spider):
        self.conn = sqlite3.connect('quotes.db')
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY,
                text TEXT,
                author TEXT,
                tags TEXT
            )
        ''')
        self.conn.commit()
    
    def close_spider(self, spider):
        self.conn.close()
    
    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        tags = ",".join(adapter.get("tags", []))
        
        self.cursor.execute('''
            INSERT INTO quotes (text, author, tags)
            VALUES (?, ?, ?)
        ''', (adapter["text"], adapter["author"], tags))
        
        self.conn.commit()
        return item


# Enable in settings.py
ITEM_PIPELINES = {
    'myproject.pipelines.SQLitePipeline': 300,
}
```

### Settings for Polite Scraping

```python
# myproject/settings.py

# Respect robots.txt
ROBOTSTXT_OBEY = True

# Rate limiting
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 8
DOWNLOAD_DELAY = 2  # seconds between requests

# User-Agent
USER_AGENT = 'MyBot/1.0 (+http://mysite.com)'

# Retry mechanism
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 408]

# Cache
HTTPCACHE_ENABLED = True
```

---

## 3. Selenium (Browser Automation)

### What it does
- Automates browser interactions
- Executes JavaScript and renders dynamic content
- Perfect for sites that load content with AJAX/React/Vue
- Can interact with forms, buttons, etc.

### Installation
```bash
pip install selenium

# Download WebDriver
# For Chrome: https://chromedriver.chromium.org/
# For Firefox: https://github.com/mozilla/geckodriver/releases
```

### Basic Example: Scrape Dynamic Content

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

# Initialize driver
driver = webdriver.Chrome("path/to/chromedriver")

try:
    # Navigate to page
    driver.get("https://example.com")
    
    # Wait for element to load (max 10 seconds)
    element = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.CLASS_NAME, "dynamic-content"))
    )
    
    # Extract data
    content = driver.find_element(By.CLASS_NAME, "dynamic-content").text
    print(content)
    
finally:
    driver.quit()
```

### Advanced: Interact with JavaScript-Heavy Site

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import time

def scrape_infinite_scroll_page(url):
    """Scrape a site with infinite scroll pagination."""
    driver = webdriver.Chrome()
    
    try:
        driver.get(url)
        items = []
        
        # Scroll to bottom and load more items
        last_height = driver.execute_script("return document.body.scrollHeight")
        
        while True:
            # Extract items from current page
            elements = driver.find_elements(By.CLASS_NAME, "item")
            for el in elements:
                items.append(el.text)
            
            # Scroll down
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)  # Wait for new content to load
            
            # Check if new content loaded
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break  # No more content
            last_height = new_height
        
        return items
    
    finally:
        driver.quit()

# Usage
items = scrape_infinite_scroll_page("https://example.com")
for item in items:
    print(item)
```

### Headless Mode (No GUI, Faster)

```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

chrome_options = Options()
chrome_options.add_argument("--headless")  # Run in background
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

driver = webdriver.Chrome(options=chrome_options)
driver.get("https://example.com")

# Scrape...

driver.quit()
```

---

## Comparison: Which Tool to Use?

### Use **BeautifulSoup** when:
- Site is static HTML
- You need a quick, simple script
- Low volume of pages
- Learning web scraping

### Use **Scrapy** when:
- Scraping hundreds/thousands of pages
- Need production-grade reliability
- Want built-in pipeline and middleware
- Project is large and long-lived

### Use **Selenium** when:
- Site heavily uses JavaScript (React, Vue, Angular)
- Need to click buttons, fill forms, interact with page
- Content loads dynamically (AJAX, infinite scroll)
- Price: speed and memory usage

---

## Best Practices

```python
# 1. Use headers to identify your scraper
headers = {
    "User-Agent": "MyBot/1.0 (+http://mysite.com)"
}

# 2. Add delays between requests
import time
time.sleep(2)  # 2 seconds between requests

# 3. Handle errors gracefully
try:
    data = element.text
except Exception as e:
    print(f"Error: {e}")

# 4. Check robots.txt
import urllib.robotparser
rp = urllib.robotparser.RobotFileParser()
rp.set_url("https://example.com/robots.txt")
rp.read()
can_fetch = rp.can_fetch("*", "https://example.com/page")

# 5. Store data immediately (don't keep in memory)
import csv
with open("data.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["name", "price"])
    writer.writeheader()
    writer.writerow({"name": "item", "price": "$10"})
```

---

## Links & Resources

- [BeautifulSoup Documentation](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- [Scrapy Documentation](https://docs.scrapy.org/)
- [Selenium Documentation](https://selenium.dev/documentation/)
- [Web Scraping with Python Book](https://automatetheboringstuff.com/2e/chapter12/)
- [Legal Guide to Web Scraping](https://blog.apify.com/is-web-scraping-legal/)

---

## Next Steps / Reflections

- Tomorrow: implement a small Scrapy project to scrape a real website
- Experiment with CSS selectors and XPath for robust extraction
- Learn about rotating proxies and handling IP bans
- Explore Scrapy middleware for custom headers and cookies
- Consider: When should I use API vs scraping?
