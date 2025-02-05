import datetime
import os
from urllib.parse import urlparse
import httpx
from selenium import webdriver
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.common.by import By
import asyncio
import crud
import re
from database import get_db
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from filter_oblast import map_location_with_region

semaphore = asyncio.Semaphore(50)
# Global flag to control scraper dynamically
SCRAPER_RUNNING = False
# Setup Selenium WebDriver using Firefox
def setup_selenium():
    service = FirefoxService(executable_path="/Users/bogdansavisev/Desktop/lviv_pject/parser/geckodriver_2")
    options = webdriver.FirefoxOptions()
    options.add_argument("--headless")  # Run in headless mode to save resources
    options.add_argument("--disable-gpu")  # Disable GPU if not needed
    options.add_argument("--no-sandbox")  # Recommended for Linux systems to avoid resource hogging
    options.add_argument("--disable-dev-shm-usage")  # Use /tmp instead of /dev/shm

    # Enable adaptive throttling to reduce resource usage on idle
    options.set_capability("pageLoadStrategy", "eager")  # Load only essential resources

    driver = webdriver.Firefox(service=service, options=options)
    return driver

BASE_URLS = [
    "https://www.olx.ua/uk/nedvizhimost/posutochno-pochasovo/posutochno-pochasovo-doma/lv/?currency=USD&page=",
    "https://www.olx.ua/uk/nedvizhimost/doma/arenda-domov/lv/?currency=USD&page=",
    "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/lv/?currency=USD&page=",
    "https://www.olx.ua/uk/nedvizhimost/kvartiry/prodazha-kvartir/lv/?currency=USD&page=",
    "https://www.olx.ua/uk/nedvizhimost/doma/prodazha-domov/lv/?currency=USD&page=",
    "https://www.olx.ua/uk/nedvizhimost/posutochno-pochasovo/posutochno-pochasovo-kvartiry/lv/?currency=USD&page=",

]

# Stage 1: Scrape apartment titles and URLs from each listings page
async def scrape_titles_and_urls(driver, base_url, page_number):
    async with semaphore:
        full_url = f"{base_url}{page_number}"
        
        print(f"Fetching page: {full_url}")
        driver.get(full_url)
        await asyncio.sleep(2)

        # Extract type_deal and type_object from the base URL
        parsed_url = urlparse(full_url)
        path_parts = parsed_url.path.split('/')
        
        # Ensure the path has enough parts to extract the values
        if len(path_parts) >= 5:
            type_deal = path_parts[3]  # Extract 'kvartiry', 'doma', etc.
            type_object = path_parts[4]  # Extract 'prodazha-kvartir', 'prodazha-domov', 'arenda-domov'
        else:
            type_deal = None
            type_object = None

        last_height = driver.execute_script("return document.body.scrollHeight")
        apartments = []

        while True:
            # Scroll down to the bottom of the page
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2)  # Wait for new elements to load

            # Collect the listings
            listings = driver.find_elements(By.CLASS_NAME, "css-1apmciz")

            for listing in listings:
                try:
                    title_element = listing.find_element(By.CSS_SELECTOR, "a.css-qo0cxu")
                    title = title_element.text
                    url = listing.find_element(By.CSS_SELECTOR, "a.css-qo0cxu").get_attribute("href")

                    # Add the title, url, type_deal, and type_object to the list
                    apartments.append({
                        "title": title,
                        "url": url,
                        "type_deal": type_deal,  # Assign type_deal extracted from base URL
                        "type_object": type_object  # Assign type_object extracted from base URL
                    })
                except Exception as e:
                    print(f"Error processing a listing: {e}")

            # Check if we've reached the bottom of the page (no more content to load)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break  # Exit the loop if no more scrolling is possible
            last_height = new_height

        return apartments

# Stage 2: Scrape additional details for each apartment from its individual page
async def scrape_apartment_details(driver, apartment_url, title, type_deal, type_object):
    driver.get(apartment_url)
    await asyncio.sleep(2)
    
    try:
        # Extract additional apartment details
        price = driver.find_element(By.CSS_SELECTOR, "h3.css-fqcbii").text
        description = driver.find_element(By.CSS_SELECTOR, "div.css-1o924a9").text
        features = driver.find_element(By.CSS_SELECTOR, "div.css-41yf00").text
        location_date_element = driver.find_element(By.CSS_SELECTOR, "div.css-13l8eec").text
        user = driver.find_element(By.CSS_SELECTOR, "h4.css-lyp0yk").text
        location_date = location_date_element.replace('Львівська область', '').strip().replace(',', '').strip()

        location_date = map_location_with_region(location_date)

        # Click on the button to reveal additional information if present
        try:
            driver.find_element(By.CSS_SELECTOR, ".css-1vgbwlu").click()
            await asyncio.sleep(1)
        except Exception as e:
            print("Button click failed or not found:", e)

        # Features extraction
        features_map = {}
        patterns = {
            "owner": r"(Приватна особа|Бізнес)",
            "residential_complex": r"Назва ЖК:\s*(.+)",
            "floor": r"Поверх:\s*(\d+)",
            "superficiality": r"Поверховість:\s*(\d+)",
            "square": r"Загальна площа:\s*(\d+)",
            "classs": r"Клас житла:\s*(.+)",
            "room": r"Кількість кімнат:\s*(\d+)"
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, features)
            features_map[key] = match.group(1) if match else ""

        # Extract phone and ID if available
        phone = driver.find_element(By.CSS_SELECTOR, "a.css-v1ndtc").text if driver.find_elements(By.CSS_SELECTOR, "a.css-v1ndtc") else None
        id_olx = driver.find_element(By.CSS_SELECTOR, "span.css-12hdxwj").text if driver.find_elements(By.CSS_SELECTOR, "span.css-12hdxwj") else None
        return {
            "title": title,
            "price": price,
            "description": description,
            "features": features,
            "location_date": location_date,
            "owner": features_map.get("owner", "Unknown"),
            "residential_complex": features_map.get("residential_complex", ""),
            "floor": features_map.get("floor", ""),
            "superficiality": features_map.get("superficiality", ""),
            "square": features_map.get("square", ""),
            "classs": features_map.get("classs", ""),
            "room": features_map.get("room", ""),
            "url": apartment_url,
            "type_deal": type_deal,
            "type_object": type_object,
            "phone": phone,
            "id_olx": id_olx,
            "user": user
        }

    except Exception as e:
        print(f"Error scraping apartment details: {e}")
        return None

async def scrape_and_save_images(driver, apartment_url, apartment_id, db: AsyncSession):
    image_dir = f"images/apartment_{apartment_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    os.makedirs(image_dir, exist_ok=True)

    driver.get(apartment_url)
    await asyncio.sleep(2)

    # Locate all image containers using the data-cy attribute
    try:
        # Select all image elements within elements marked with data-cy="adPhotos-swiperSlide"
        image_containers = driver.find_elements(By.CSS_SELECTOR, 'div[data-cy="adPhotos-swiperSlide"]')
        
        async with httpx.AsyncClient() as client:
            for index, container in enumerate(image_containers):
                img_element = container.find_element(By.TAG_NAME, "img")
                img_url = img_element.get_attribute("src")
                
                if img_url:
                    # Download and save each image
                    response = await client.get(img_url)
                    image_path = f"{image_dir}/image_{index + 1}.jpg"  # Start index from 1
                    with open(image_path, 'wb') as handler:
                        handler.write(response.content)

                    # Prepare image data with an order field
                    image_data = {
                        "filename": f"image_{index + 1}.jpg",  # Start filename index from 1
                        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "content_type": "image/jpeg",
                        "file_path": image_path,
                        "order": index + 1  # Set order starting from 1
                    }

                    # Save image information to the database
                    await crud.add_image_to_apartment(db=db, apartment_id=apartment_id, image_data=image_data)

    except Exception as e:
        print(f"Error saving images: {e}")



async def scrape_and_save(total_pages=5):
    """Main scraper function that stops dynamically when `SCRAPER_RUNNING` is set to False."""
    global SCRAPER_RUNNING

    if not SCRAPER_RUNNING:
        print("🚫 Scraper is stopped. Exiting...")
        return  # Stop if scraper is not running

    driver = setup_selenium()
    
    async for db in get_db():
        try:
            results_by_url = {}

            # Check if BASE_URLS is empty
            if not BASE_URLS:
                print("🚫 BASE_URLS is empty. Stopping scraper.")
                SCRAPER_RUNNING = False  # Reset flag
                return

            for base_url in BASE_URLS:
                if not SCRAPER_RUNNING:
                    print("🚫 Scraper stopped while running.")
                    return  

                url_results = []
                for page in range(1, total_pages + 1):
                    if not SCRAPER_RUNNING:
                        print("🚫 Scraper stopped while fetching pages.")
                        return 

                    apartments = await scrape_titles_and_urls(driver, base_url, page)
                    url_results.extend(apartments)
                results_by_url[base_url] = url_results

            # Process the scraped apartments
            for base_url, apartments in results_by_url.items():
                print(f"Processing apartments for URL: {base_url}")

                for apartment in apartments:
                    if not SCRAPER_RUNNING:
                        print("🚫 Scraper stopped while processing apartments.")
                        return  

                    details = await scrape_apartment_details(
                        driver, apartment['url'], apartment['title'], 
                        apartment['type_deal'], apartment['type_object']
                    )

                    if details:
                        try:
                            saved_apartment = await crud.create_or_update_apartment(db, details)
                            apartment_id = saved_apartment.id

                            await scrape_and_save_images(driver, apartment['url'], apartment_id, db)
                        except Exception as e:
                            print(f"❌ Error saving apartment details or images: {e}")

        finally:
            driver.quit()
            await db.close()
            SCRAPER_RUNNING = False 