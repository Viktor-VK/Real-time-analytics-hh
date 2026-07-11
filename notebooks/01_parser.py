#!/usr/bin/env python
# coding: utf-8

# # 🔍 Анализ рынка аналитических вакансий (HH.ru)
# 
# **Автор:** Виктор Кобцев  
# **Дата создания:** 13.06.2026  
# **Версия:** 1.0
# 
# ---
# 
# ## 📋 Описание проекта
# 
# Автономный ETL-пайплайн для мониторинга вакансий на HH.ru.  
# Парсер ежедневно собирает данные, сохраняет исторические "слепки" в локальную базу DuckDB,  
# на основе которых строится аналитический дашборд в Apache Superset.
# 
# **Цели проекта:**
# - Анализ динамики рынка аналитических вакансий
# - Мониторинг востребованных навыков и технологий
# - Исследование зарплатных диапазонов по регионам и уровням
# 
# ---
# 
# ## 🗂️ Структура проекта
# 
# ```
# hh_analitics/
# ├── data/          # База данных DuckDB
# ├── notebooks/     # Jupyter ноутбуки
# │   └── 01_parser.ipynb
# └── logs/          # Логи парсера
# ```
# 
# ## 1. Импорт библиотек

# In[11]:


import re
import pandas as pd
import duckdb
from datetime import datetime
import time
import random
import json
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from itertools import product

print("Все библиотеки загружены успешно!")
print(f"Pandas версия: {pd.__version__}")
print(f"DuckDB версия: {duckdb.__version__}")
print(f"Текущее время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ## 2. Настройка параметров

# In[2]:


# Базовый URL для поиска
BASE_URL = "https://hh.ru/search/vacancy"

# Пути
DB_PATH = "/opt/hh_analitics/data/hh_vacancies.duckdb"
LOG_PATH = "/opt/hh_analitics/logs/parser.log"

# Параметры поиска — декартово произведение сегментов
AREAS = ["1", "113&excluded_area=1"]
EXPERIENCE = ["noExperience", "between1And3", "between3And6", "moreThan6"]
WORK_FORMATS = ["ON_SITE", "REMOTE&work_format=HYBRID&work_format=FIELD_WORK"]
HAS_SALARY = ["true", "false"]

SEGMENTS = list(product(AREAS, EXPERIENCE, WORK_FORMATS, HAS_SALARY))
# SEGMENTS = [("1", "between1And3", "ON_SITE", "true")]  #test

print(f"Всего сегментов: {len(SEGMENTS)}")
for i, (area, exp, fmt, has_salary) in enumerate(SEGMENTS):
    print(f"  {i+1}. area={area} | exp={exp} | format={fmt} | has_salary={has_salary}")

# ## 3. Парсинг вакансий с HH.ru

# In[3]:


def create_driver():
    """Создаёт и настраивает браузер Chrome"""
    
    options = Options()
    options.add_argument("--headless")           # без графического интерфейса
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)  # не ждать загрузку страницы дольше 30 секунд
    print("Браузер запущен в фоновом режиме")
    return driver
    return driver

# Проверяем
driver = create_driver()
print(f"Статус: OK")
driver.quit()
print("Браузер закрыт")

# In[4]:


def get_salary(card):
    """Извлекает сырой текст зарплаты"""
    spans = card.find_all("span")
    for span in spans:
        text = span.get_text(strip=True)
        if "₽" in text and len(text) < 60 and len(span.find_all("span")) == 0:
            return text
    return None

def parse_salary(salary_raw):
    """Парсит зарплату: извлекает все числа, min -> from, max -> to"""
    if not salary_raw:
        return None, None, None
    
    # Определяем валюту
    currency = "RUB" if "₽" in salary_raw else None
    
    # Извлекаем все числа (убираем пробелы внутри чисел типа "15 000")
    numbers = re.findall(r'\d[\d\s]*\d|\d+', salary_raw)
    numbers = [int(n.replace(' ', '').replace('\u202f', '').replace('\xa0', '')) for n in numbers]
    
    # Фильтруем явно нереалистичные значения (например, год в тексте)
    numbers = [n for n in numbers if 1000 <= n <= 10_000_000]
    
    if not numbers:
        return None, None, currency
    
    salary_from = min(numbers)
    salary_to = max(numbers)
    
    return salary_from, salary_to, currency

def parse_vacancy_card(card):
    """Извлекает данные из одной карточки вакансии"""
    try:
        # Название
        title = card.find("a", {"data-qa": "serp-item__title"})
        title = title.text.strip() if title else None

        # Ссылка
        link = card.find("a", {"data-qa": "serp-item__title"})
        link = link.get("href") if link else None

        # Компания
        company = card.find(attrs={"data-qa": "vacancy-serp__vacancy-employer"})
        company = company.text.strip() if company else None

        # Город
        city = card.find(attrs={"data-qa": "vacancy-serp__vacancy-address"})
        city = city.text.strip() if city else None

        # Опыт
        exp = card.find(attrs={"data-qa": lambda x: x and "work-experience" in x})
        exp = exp.text.strip() if exp else None

        # Удалёнка
        remote = card.find(attrs={"data-qa": lambda x: x and "work-schedule-remote" in x})
        is_remote = True if remote else False

        # Отклик
        responded = card.find(attrs={"data-qa": "vacancy-serp__vacancy_responded"})
        is_applied = True if responded else False

        # Зарплата
        salary_raw = get_salary(card)
        salary_from, salary_to, salary_currency = parse_salary(salary_raw)

        return {
            "title": title,
            "company": company,
            "city": city,
            "experience": exp,
            "is_remote": is_remote,
            "is_applied": is_applied,
            "salary_raw": salary_raw,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": salary_currency,
            "url": link,
            "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
        }

    except Exception as e:
        print(f"Ошибка парсинга карточки: {e}")
        return None

print("Функции парсинга обновлены!")

# In[10]:


def fetch_vacancies(segments):
    """Собирает вакансии по всем сегментам (регион × опыт × формат × зарплата)"""
    from tqdm import tqdm
    from selenium.common.exceptions import TimeoutException, WebDriverException

    driver = create_driver()
    all_vacancies = []

    print(f"\n=== Начинаем сбор: {len(segments)} сегментов ===", flush=True)

    try:
        with tqdm(total=len(segments), desc="Сбор вакансий", unit="сегм") as pbar:
            for idx, (area, experience, work_format, has_salary) in enumerate(segments, start=1):
                segment_count = 0

                for page in range(40):
                    url = (
                        f"{BASE_URL}"
                        f"?text=аналитик+OR+analyst+OR+analytics"
                        f"&search_field=name"
                        f"&area={area}"
                        f"&experience={experience}"
                        f"&work_format={work_format}"
                        f"&salary=1"
                        f"&only_with_salary={has_salary}"
                        f"&page={page}"
                    )

                    loaded = False
                    for attempt in range(2):
                        try:
                            driver.get(url)
                            loaded = True
                            break
                        except (TimeoutException, WebDriverException) as e:
                            print(
                                f"  [!] Ошибка загрузки (сегмент {idx}/{len(segments)}, "
                                f"page={page}, попытка {attempt + 1}/2): {type(e).__name__}",
                                flush=True,
                            )
                            time.sleep(3)

                    if not loaded:
                        print(
                            f"  [!] Пропускаем страницу {page} сегмента {idx}/{len(segments)} "
                            f"— не удалось загрузить после 2 попыток",
                            flush=True,
                        )
                        continue

                    time.sleep(random.uniform(2, 4))

                    soup = BeautifulSoup(driver.page_source, "lxml")
                    cards = soup.find_all("div", {"data-qa": "vacancy-serp__vacancy"})

                    if not cards:
                        if page == 0:
                            page_title = driver.title
                            page_text_snippet = soup.get_text(separator=" ", strip=True)[:300]
                            print(
                                f"  [!] Сегмент {idx}/{len(segments)}: 0 карточек уже на "
                                f"первой странице (area={area}, exp={experience}, "
                                f"format={work_format}, has_salary={has_salary}) — "
                                f"возможна блокировка/капча",
                                flush=True,
                            )
                            print(f"      title страницы: {page_title!r}", flush=True)
                            print(f"      текст страницы (первые 300 симв.): {page_text_snippet!r}", flush=True)
                        break

                    for card in cards:
                        vacancy = parse_vacancy_card(card)
                        if vacancy:
                            all_vacancies.append(vacancy)
                            segment_count += 1

                    time.sleep(random.uniform(2, 5))

                print(
                    f"Сегмент {idx}/{len(segments)} "
                    f"(area={area}, exp={experience}, format={work_format}, has_salary={has_salary}): "
                    f"собрано {segment_count} | накоплено всего {len(all_vacancies)}",
                    flush=True,
                )

                pbar.set_postfix({"собрано": len(all_vacancies)})
                pbar.update(1)

                # Пауза между сегментами — "остывание" перед следующей пачкой запросов
                cooldown = random.uniform(8, 20)
                print(f"  ... пауза между сегментами: {cooldown:.1f} сек", flush=True)
                time.sleep(cooldown)

    finally:
        driver.quit()

    print(f"\nИтого собрано: {len(all_vacancies)} вакансий")
    return all_vacancies

# ## 4. Сохранение в базу данных

# In[6]:


def save_to_db(df, db_path):
    con = duckdb.connect(db_path)
    
    # con.execute("DROP TABLE IF EXISTS vacancies")  # !!!Удаление базы!!!
    
    df = df.drop_duplicates(subset=['url'])
    
    con.execute("""
        CREATE TABLE IF NOT EXISTS vacancies (
            title VARCHAR,
            company VARCHAR,
            city VARCHAR,
            experience VARCHAR,
            is_remote BOOLEAN,
            is_applied BOOLEAN,
            salary_raw VARCHAR,
            salary_from DOUBLE,
            salary_to DOUBLE,
            salary_currency VARCHAR,
            url VARCHAR,
            snapshot_date DATE,
            date_last_seen DATE
        )
    """)
    
    # Новые вакансии — которых ещё нет в базе
    con.execute("""
        INSERT INTO vacancies 
        SELECT *, snapshot_date as date_last_seen FROM df
        WHERE url NOT IN (SELECT url FROM vacancies)
    """)
    
    # Обновляем date_last_seen для уже существующих
    con.execute("""
        UPDATE vacancies
        SET date_last_seen = CURRENT_DATE
        WHERE url IN (SELECT url FROM df)
    """)
    
    count = con.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0]
    today = con.execute("SELECT COUNT(*) FROM vacancies WHERE date_last_seen = CURRENT_DATE").fetchone()[0]
    print(f"Всего записей в базе: {count}")
    print(f"Активных сегодня: {today}")
    
    con.close()
    print("Данные сохранены!")

# ## 5. Запуск полного пайплайна

# In[7]:


import logging

def setup_logger(log_path):
    """Настраивает логгер"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

def run_pipeline():
    """Главная функция — запускает весь ETL пайплайн"""
    logger = setup_logger(LOG_PATH)
    logger.info("=== Запуск пайплайна ===")
    
    try:
        # Extract
        logger.info("Шаг 1: Парсинг вакансий...")
        raw_vacancies = fetch_vacancies(SEGMENTS)
        logger.info(f"Собрано вакансий: {len(raw_vacancies)}")
        
        # Transform
        logger.info("Шаг 2: Формирование датафрейма...")
        df = pd.DataFrame(raw_vacancies)
        df = df.dropna(subset=["title"])
        logger.info(f"Датафрейм: {df.shape[0]} строк, {df.shape[1]} колонок")
        
        # Load
        logger.info("Шаг 3: Сохранение в базу данных...")
        save_to_db(df, DB_PATH)
        
        logger.info("=== Пайплайн завершён успешно ===")
        return df
    
    except Exception as e:
        logger.error(f"Ошибка пайплайна: {e}")
        raise


# In[8]:


# Ручной запуск: только парсинг (без записи в БД)
logger = setup_logger(LOG_PATH)
raw_vacancies = fetch_vacancies(SEGMENTS)
df = pd.DataFrame(raw_vacancies)
df = df.dropna(subset=["title"])
logger.info(f"Датафрейм готов: {df.shape[0]} строк")

# In[9]:


# Ручной запуск: только запись в БД
save_to_db(df, DB_PATH)
logger.info("=== Данные сохранены ===")
