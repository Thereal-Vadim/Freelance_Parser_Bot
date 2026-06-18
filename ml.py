import os
import json
import logging
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline
import db

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "vacancy_model.pkl")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def train_model():
    """
    Обучает модель Naive Bayes на исторических данных, если достигнут минимальный порог.
    Возвращает строку с результатом или ошибкой.
    """
    config = load_config()
    min_samples = config.get("ml_min_samples_per_class", 30)

    X, y = db.get_training_data()
    
    if not X:
        return "Нет данных для обучения."

    approved_count = sum(y)
    rejected_count = len(y) - approved_count

    if approved_count < min_samples or rejected_count < min_samples:
        msg = f"Недостаточно данных для обучения. Нужно по {min_samples} примеров каждого класса. Сейчас: Approved={approved_count}, Rejected={rejected_count}."
        logger.info(msg)
        return msg

    # Создаем pipeline: Векторизация -> Классификатор
    pipeline = Pipeline([
        ('tfidf', TfidfVectorizer(max_features=5000, ngram_range=(1, 2))),
        ('clf', MultinomialNB())
    ])

    pipeline.fit(X, y)
    
    joblib.dump(pipeline, MODEL_PATH)
    msg = f"Модель успешно обучена на {approved_count} одобренных и {rejected_count} отклоненных вакансиях."
    logger.info(msg)
    return msg

def predict_job(title, description):
    """
    Предсказывает вероятность того, что вакансия будет одобрена.
    Возвращает вероятность (float от 0.0 до 1.0) или None, если модель еще не обучена.
    """
    if not os.path.exists(MODEL_PATH):
        return None

    try:
        pipeline = joblib.load(MODEL_PATH)
    except Exception as e:
        logger.error(f"Ошибка при загрузке модели ML: {e}")
        return None

    text = f"{title} {description}".strip()
    
    # predict_proba возвращает массив вероятностей для каждого класса (0 и 1)
    # Нас интересует вероятность класса 1 (Approved)
    probabilities = pipeline.predict_proba([text])[0]
    
    # probabilities[1] - это вероятность класса 1 (approved)
    return float(probabilities[1])
