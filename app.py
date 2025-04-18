import os
import json
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_socketio import SocketIO, emit
from openai import OpenAI
from dotenv import load_dotenv
from functools import wraps
import asyncio
import time
import uuid
import re
from urllib.parse import urlparse

# Загружаем переменные окружения из .env файла
load_dotenv()

# Получаем API ключ из переменных окружения
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY не задан в переменных окружения")

# Инициализируем клиента OpenAI
client = OpenAI(api_key=api_key)

# Инициализация Flask и Socket.IO
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))
socketio = SocketIO(app, cors_allowed_origins="*")

# Отслеживание активных задач генерации
active_tasks = {}

# Список доступных типов элементов
ELEMENT_TYPES = {
    "contact": "Контактные данные",
    "product": "Карточка товара",
    "catalog": "Каталог товаров",
    "breadcrumbs": "Хлебные крошки",
    "searchform": "Форма поиска",
    "logo": "Логотип",
    "faq": "Часто задаваемые вопросы",
    "article": "Статья",
    "qapage": "QA страница",
    "organization": "Организация"
}

# Декоратор для валидации URL
def validate_url(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'POST':
            url = request.json.get('url', '')
            if not url.startswith(('http://', 'https://')):
                return jsonify({"error": "URL должен начинаться с http:// или https://"}), 400
        return f(*args, **kwargs)
    return decorated_function

async def generate_microdata_template(element_type: str, url: str, session_id: str = None) -> str:
    """
    Генерирует шаблон микроразметки с помощью OpenAI API на основе типа элемента и URL,
    используя возможность веб-поиска для анализа сайта
    """
    if element_type not in ELEMENT_TYPES:
        return f"Неизвестный тип элемента: {element_type}"
    
    # Отправляем статус начала генерации
    if session_id:
        socketio.emit('generation_status', {
            'session_id': session_id,
            'element_type': element_type,
            'status': 'started',
            'message': f'Начало генерации микроразметки для {ELEMENT_TYPES[element_type]}...'
        })
    
    # Определяем доменное имя из URL для более персонализированного контента
    domain = urlparse(url).netloc
    
    # Определяем промпты для разных типов микроразметки
    schema_prompts = {
        "contact": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для контактных данных компании. Используй тип LocalBusiness, включи настоящее название компании, адрес, телефон, email и часы работы, которые ты найдешь на сайте. Если что-то не найдешь, оставь поля пустыми или с заглушками.",
        
        "product": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для карточки товара. Используй тип Product, включи название товара, описание, бренд, изображение, артикул (SKU), цену, валюту и наличие, которые ты найдешь на сайте. Если это страница товара, используй информацию с нее.",
        
        "catalog": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для категории товаров с использованием AggregateOffer. Включи название категории, описание, диапазон цен (lowPrice и highPrice), количество товаров (offerCount), а также рейтинг (ratingValue должен быть выше 4.5, с рандомным значением между 4.5 и 5.0, reviewCount - рандомное значение между 50 и 500). Используй структуру, где тип страницы будет CollectionPage, а внутри будет Product с AggregateOffer. Найди на странице реальные цены товаров для указания диапазона цен.",
        
        "breadcrumbs": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для хлебных крошек, основываясь на настоящей структуре навигации сайта. Используй тип BreadcrumbList с реальными разделами сайта. Очень важно: в предпоследнем сегменте хлебных крошек (обычно это категория или раздел) обязательно добавь подходящие по смыслу эмоджи перед названием (например, для раздела 'Apple' добавь '📱📱📱 Apple'). Выбери эмоджи, соответствующие содержимому раздела.",
        
        "searchform": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для поисковой формы сайта. Используй тип WebSite с potentialAction типа SearchAction, основываясь на реальном URL поиска сайта.",
        
        "logo": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для логотипа организации. Используй тип Organization и URL логотипа, который ты найдешь на сайте.",
        
        "faq": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для страницы FAQ (часто задаваемых вопросов). Используй тип FAQPage с 3-5 реальными вопросами и ответами, найденными на сайте.",
        
        "article": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для статьи. Используй тип Article, включи заголовок, описание, автора, издателя, дату публикации и изображение с реального сайта.",
        
        "qapage": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для QAPage. Используй тип QAPage с одним вопросом и ответом. Вопросом должно быть название категории товаров или услуг, найденное на странице. Обязательно включи поле 'answerCount' со значением 1. Ответ должен включать УТП (уникальные торговые предложения) компании, которые ты найдешь на сайте (например, 'Высокое качество', 'Доставка по всей России', 'Выгодная бонусная система'). В ответе обязательно используй соответствующие эмоджи (например, ⭐ 💎 🚚). Укажи upvoteCount между 20 и 30, а URL ответа должен совпадать с URL текущей страницы.",
        
        "organization": f"Посети сайт {url} и создай микроразметку Schema.org JSON-LD для организации. Используй тип Organization, включи настоящее название организации, адрес, контактные данные, ссылки на социальные сети, которые ты найдешь на сайте."
    }
    
    # Отправляем статус о начале запроса к API
    if session_id:
        socketio.emit('generation_status', {
            'session_id': session_id,
            'element_type': element_type,
            'status': 'processing',
            'message': f'Анализ сайта {domain} и создание микроразметки {ELEMENT_TYPES[element_type]}...'
        })
    
    # Отправляем запрос к OpenAI API для генерации микроразметки
    try:
        # Используем модель с возможностью веб-поиска
        response = client.chat.completions.create(
            model="gpt-4o-search-preview",  # Модель с поддержкой веб-поиска
            web_search_options={
                "search_context_size": "medium"  # Средний размер контекста для баланса скорости и качества
            },
            messages=[
                {"role": "system", "content": "Ты эксперт по Schema.org и микроразметке JSON-LD. Твоя задача - создавать качественные шаблоны микроразметки для разных типов контента на основе анализа реального содержимого сайта. Твои шаблоны должны быть максимально детализированными, соответствовать стандартам Schema.org и рекомендациям поисковых систем. Всегда возвращай только код JSON-LD без дополнительных пояснений, обернутый в тег script."},
                {"role": "user", "content": schema_prompts[element_type]}
            ],
            max_tokens=2000
        )
        
        # Извлекаем ответ и проверяем его корректность
        generated_content = response.choices[0].message.content.strip()
        
        # Проверяем, содержит ли ответ URL-цитаты
        if hasattr(response.choices[0].message, 'annotations') and response.choices[0].message.annotations:
            # Логгируем информацию о цитируемых источниках
            for annotation in response.choices[0].message.annotations:
                if annotation.type == 'url_citation':
                    print(f"Использован источник: {annotation.url_citation.url} - {annotation.url_citation.title}")
        
        # Убеждаемся, что ответ содержит тег script
        if not generated_content.startswith('<script'):
            # Ищем начало JSON (для случаев, когда модель вернула JSON без тега script)
            json_match = re.search(r'({[\s\S]*})', generated_content)
            if json_match:
                json_content = json_match.group(1)
                generated_content = f'<script type="application/ld+json">\n{json_content}\n</script>'
            else:
                generated_content = f'<script type="application/ld+json">\n{generated_content}\n</script>'
        
        # Если в ответе нет обрамляющих тегов script, добавляем их
        if 'application/ld+json' not in generated_content:
            if generated_content.startswith('{') and (generated_content.endswith('}') or generated_content.endswith('}\n')):
                generated_content = f'<script type="application/ld+json">\n{generated_content}\n</script>'
        
        # Отправляем статус о завершении генерации
        if session_id:
            socketio.emit('generation_status', {
                'session_id': session_id,
                'element_type': element_type,
                'status': 'completed',
                'message': f'Микроразметка для {ELEMENT_TYPES[element_type]} успешно сгенерирована на основе анализа сайта'
            })
        
        return generated_content
        
    except Exception as e:
        # В случае ошибки отправляем статус об ошибке
        if session_id:
            socketio.emit('generation_status', {
                'session_id': session_id,
                'element_type': element_type,
                'status': 'error',
                'message': f'Ошибка при генерации {ELEMENT_TYPES[element_type]}: {str(e)}'
            })
            
        print(f"Ошибка при генерации микроразметки через OpenAI: {str(e)}")
        
        # Используем базовые шаблоны как запасной вариант
        fallback_templates = {
            "contact": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "LocalBusiness",
  "name": "Компания с сайта {domain}",
  "url": "{url}",
  "telephone": "+7 (XXX) XXX-XX-XX",
  "email": "info@{domain}",
  "address": {{
    "@type": "PostalAddress",
    "streetAddress": "Адрес компании",
    "addressLocality": "Москва",
    "postalCode": "101000",
    "addressCountry": "RU"
  }}
}}
</script>""",
            
            "product": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org/",
  "@type": "Product",
  "name": "Название товара",
  "image": "{url}/image.jpg",
  "description": "Описание товара с сайта {domain}",
  "brand": {{
    "@type": "Brand",
    "name": "Бренд"
  }},
  "offers": {{
    "@type": "Offer",
    "url": "{url}",
    "priceCurrency": "RUB",
    "price": "0",
    "availability": "https://schema.org/InStock"
  }}
}}
</script>""",

            "catalog": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "CollectionPage",
  "name": "Каталог товаров",
  "description": "Раздел с товарами на сайте {domain}",
  "mainEntity": {{
    "@type": "Product",
    "name": "Товары с сайта {domain}",
    "offers": {{
      "@type": "AggregateOffer",
      "lowPrice": "5000",
      "highPrice": "150000",
      "priceCurrency": "RUB",
      "offerCount": "125",
      "availability": "https://schema.org/InStock"
    }},
    "aggregateRating": {{
      "@type": "AggregateRating",
      "ratingValue": "4.8",
      "reviewCount": "324"
    }}
  }}
}}
</script>""",

            "breadcrumbs": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {{
      "@type": "ListItem",
      "position": 1,
      "name": "Главная",
      "item": "{url}"
    }},
    {{
      "@type": "ListItem",
      "position": 2,
      "name": "Каталог",
      "item": "{url}/catalog/"
    }},
    {{
      "@type": "ListItem",
      "position": 3,
      "name": "📦📦📦 Категория товаров",
      "item": "{url}/catalog/category/"
    }},
    {{
      "@type": "ListItem",
      "position": 4,
      "name": "Подкатегория",
      "item": "{url}/catalog/category/subcategory/"
    }}
  ]
}}
</script>""",

            "searchform": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "WebSite",
  "url": "{url}",
  "potentialAction": {{
    "@type": "SearchAction",
    "target": "{url}/search?q={{search_term_string}}",
    "query-input": "required name=search_term_string"
  }}
}}
</script>""",

            "logo": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Organization",
  "url": "{url}",
  "logo": "{url}/logo.png"
}}
</script>""",

            "faq": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {{
      "@type": "Question",
      "name": "Как сделать заказ?",
      "acceptedAnswer": {{
        "@type": "Answer",
        "text": "Для оформления заказа добавьте товары в корзину и следуйте инструкциям на экране."
      }}
    }},
    {{
      "@type": "Question",
      "name": "Какие способы доставки вы предлагаете?",
      "acceptedAnswer": {{
        "@type": "Answer",
        "text": "Мы предлагаем курьерскую доставку, самовывоз из наших пунктов выдачи и доставку почтой."
      }}
    }},
    {{
      "@type": "Question",
      "name": "Как вернуть товар?",
      "acceptedAnswer": {{
        "@type": "Answer",
        "text": "Вы можете вернуть товар в течение 14 дней с момента покупки при сохранении товарного вида."
      }}
    }}
  ]
}}
</script>""",

            "article": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "Заголовок статьи",
  "description": "Описание статьи",
  "image": "{url}/image.jpg",
  "datePublished": "2023-01-01T10:00:00+03:00",
  "dateModified": "2023-01-01T12:00:00+03:00",
  "author": {{
    "@type": "Person",
    "name": "Автор статьи"
  }},
  "publisher": {{
    "@type": "Organization",
    "name": "Компания с сайта {domain}",
    "logo": {{
      "@type": "ImageObject",
      "url": "{url}/logo.png"
    }}
  }}
}}
</script>""",

            "qapage": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "QAPage",
  "mainEntity": {{
    "@type": "Question",
    "name": "Товары",
    "answerCount": 1,
    "upvoteCount": 26,
    "datePublished": "{time.strftime('%Y-%m-%dT%H:%M:%S')}",
    "acceptedAnswer": {{
      "@type": "Answer",
      "url": "{url}",
      "text": "⭐ Высокое качество товаров 💎 Доставка по всей России 💎 Выгодная бонусная система",
      "upvoteCount": 26,
      "datePublished": "{time.strftime('%Y-%m-%dT%H:%M:%S')}"
    }}
  }}
}}
</script>""",

            "organization": f"""<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "Компания с сайта {domain}",
  "url": "{url}",
  "logo": "{url}/logo.png",
  "contactPoint": {{
    "@type": "ContactPoint",
    "telephone": "+7 (XXX) XXX-XX-XX",
    "contactType": "customer service",
    "availableLanguage": ["Russian"]
  }},
  "address": {{
    "@type": "PostalAddress",
    "streetAddress": "Адрес компании",
    "addressLocality": "Москва",
    "postalCode": "101000",
    "addressCountry": "RU"
  }},
  "sameAs": [
    "https://vk.com/company",
    "https://t.me/company"
  ]
}}
</script>"""
        }
        
        return fallback_templates.get(element_type, f"<script type='application/ld+json'>{{'@context': 'https://schema.org', '@type': 'Thing', 'url': '{url}'}}</script>")

# Маршрут для генерации шаблона через OpenAI API
@app.route('/generate-template', methods=['POST'])
@validate_url
def generate_template():
    try:
        data = request.get_json()
        element_type = data.get("element_type")
        url = data.get("url")
        
        # Извлекаем домен из URL
        domain = urlparse(url).netloc
        
        # Создаем уникальный идентификатор сессии
        session_id = str(uuid.uuid4())
        
        # Проверка валидности типа элемента
        if element_type not in ELEMENT_TYPES:
            return jsonify({"error": f"Недопустимый тип элемента: {element_type}"}), 400
        
        # Регистрируем задачу
        active_tasks[session_id] = {
            'element_type': element_type,
            'url': url,
            'domain': domain,
            'status': 'starting',
            'start_time': time.time()
        }
        
        # Запускаем фоновую задачу
        @socketio.start_background_task
        def background_task():
            try:
                # Выполняем асинхронную генерацию в отдельном event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                generated = loop.run_until_complete(generate_microdata_template(element_type, url, session_id))
                loop.close()
                
                # Обновляем статус задачи
                active_tasks[session_id]['status'] = 'completed'
                active_tasks[session_id]['result'] = generated
                active_tasks[session_id]['end_time'] = time.time()
                
                # Отправляем результат через сокет
                socketio.emit('generation_result', {
                    'session_id': session_id,
                    'result': generated,
                    'domain': domain
                })
            except Exception as e:
                # Обновляем статус задачи при ошибке
                active_tasks[session_id]['status'] = 'error'
                active_tasks[session_id]['error'] = str(e)
                
                # Отправляем ошибку через сокет
                socketio.emit('generation_error', {
                    'session_id': session_id,
                    'error': str(e)
                })
        
        # Возвращаем идентификатор сессии для отслеживания
        return jsonify({
            "session_id": session_id,
            "message": "Задача генерации запущена, используйте session_id для отслеживания статуса"
        })
            
    except Exception as e:
        return jsonify({"error": f"Общая ошибка: {str(e)}"}), 500

# Маршрут для проверки статуса задачи
@app.route('/generation-status/<session_id>', methods=['GET'])
def check_generation_status(session_id):
    if session_id in active_tasks:
        task = active_tasks[session_id]
        
        if task['status'] == 'completed':
            # Если задача выполнена, возвращаем результат
            return jsonify({
                "status": "completed",
                "result": task.get('result', ''),
                "execution_time": task['end_time'] - task['start_time']
            })
        elif task['status'] == 'error':
            # Если произошла ошибка, возвращаем информацию об ошибке
            return jsonify({
                "status": "error",
                "error": task.get('error', 'Неизвестная ошибка'),
                "execution_time": time.time() - task['start_time']
            })
        else:
            # Если задача все еще выполняется
            return jsonify({
                "status": task['status'],
                "message": "Задача все еще выполняется",
                "elapsed_time": time.time() - task['start_time']
            })
    else:
        return jsonify({"error": "Задача не найдена"}), 404

# Socket.IO обработчики
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

# Генерация всех типов микроразметки для одного URL
@app.route('/generate-all', methods=['POST'])
@validate_url
def generate_all():
    try:
        data = request.get_json()
        url = data.get("url")
        
        if not url:
            return jsonify({"error": "URL не указан"}), 400
        
        # Создаем уникальный идентификатор сессии
        session_id = str(uuid.uuid4())
        
        # Регистрируем задачу
        active_tasks[session_id] = {
            'element_types': list(ELEMENT_TYPES.keys()),
            'url': url,
            'status': 'starting',
            'start_time': time.time(),
            'progress': 0,
            'total': len(ELEMENT_TYPES),
            'results': {}
        }
        
        # Запускаем фоновую задачу
        @socketio.start_background_task
        def background_task():
            try:
                # Создаем и запускаем асинхронную функцию для генерации всех шаблонов
                async def generate_all_async():
                    results = {}
                    # Генерируем шаблоны последовательно
                    for i, element_type in enumerate(ELEMENT_TYPES):
                        try:
                            # Обновляем прогресс
                            active_tasks[session_id]['progress'] = i
                            active_tasks[session_id]['current_element'] = element_type
                            
                            # Генерируем микроразметку
                            template = await generate_microdata_template(element_type, url, session_id)
                            results[ELEMENT_TYPES[element_type]] = template
                            
                            # Обновляем результаты в реальном времени
                            active_tasks[session_id]['results'][ELEMENT_TYPES[element_type]] = template
                            
                            # Отправляем прогресс через сокет
                            socketio.emit('generation_progress', {
                                'session_id': session_id,
                                'progress': i + 1,
                                'total': len(ELEMENT_TYPES),
                                'current': ELEMENT_TYPES[element_type],
                                'completed': list(results.keys())
                            })
                            
                        except Exception as e:
                            results[ELEMENT_TYPES[element_type]] = f"Ошибка: {str(e)}"
                    return results
                
                # Выполняем асинхронную генерацию в отдельном event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                generated_code = loop.run_until_complete(generate_all_async())
                loop.close()
                
                # Обновляем статус задачи
                active_tasks[session_id]['status'] = 'completed'
                active_tasks[session_id]['results'] = generated_code
                active_tasks[session_id]['end_time'] = time.time()
                
                # Отправляем результат через сокет
                socketio.emit('all_generation_result', {
                    'session_id': session_id,
                    'results': generated_code
                })
            except Exception as e:
                # Обновляем статус задачи при ошибке
                active_tasks[session_id]['status'] = 'error'
                active_tasks[session_id]['error'] = str(e)
                
                # Отправляем ошибку через сокет
                socketio.emit('generation_error', {
                    'session_id': session_id,
                    'error': str(e)
                })
        
        # Возвращаем идентификатор сессии для отслеживания
        return jsonify({
            "session_id": session_id,
            "message": "Задача генерации всех шаблонов запущена, используйте session_id для отслеживания статуса"
        })
        
    except Exception as e:
        return jsonify({"error": f"Ошибка при генерации всех шаблонов: {str(e)}"}), 500

# Главная страница с формой
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html', element_types=ELEMENT_TYPES)

# Страница результатов для всех шаблонов
@app.route('/results', methods=['GET', 'POST'])
def results():
    if request.method == 'POST':
        url = request.form.get('url')
        
        if not url:
            flash('URL не указан', 'error')
            return redirect(url_for('index'))
            
        if not url.startswith(('http://', 'https://')):
            flash('URL должен начинаться с http:// или https://', 'error')
            return redirect(url_for('index'))
        
        # Перенаправляем на страницу ожидания с параметром URL
        return render_template('waiting.html', url=url, element_types=ELEMENT_TYPES)
        
    return redirect(url_for('index'))

# Маршрут для получения результатов после ожидания
@app.route('/get-results/<session_id>', methods=['GET'])
def get_results(session_id):
    if session_id in active_tasks and active_tasks[session_id]['status'] == 'completed':
        url = active_tasks[session_id]['url']
        domain = urlparse(url).netloc
        generated_code = active_tasks[session_id]['results']
        return render_template('result.html', generated_code=generated_code, url=url, domain=domain)
    else:
        flash('Задача генерации не найдена или не завершена', 'error')
        return redirect(url_for('index'))

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5003)