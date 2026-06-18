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
    Trains the Naive Bayes model on historical data if the minimum threshold is met.
    Returns a string containing the result or error message.
    """
    config = load_config()
    min_samples = config.get("ml_min_samples_per_class", 30)

    X, y = db.get_training_data()
    
    if not X:
        return "No data available for training."

    approved_count = sum(y)
    rejected_count = len(y) - approved_count

    if approved_count < min_samples or rejected_count < min_samples:
        msg = f"Insufficient data for training. Need at least {min_samples} samples of each class. Current status: Approved={approved_count}, Rejected={rejected_count}."
        logger.info(msg)
        return msg

    # Create a pipeline: Vectorization -> Classifier
    pipeline = Pipeline([
        ('tfidf', TfidfVectorizer(max_features=5000, ngram_range=(1, 2))),
        ('clf', MultinomialNB())
    ])

    pipeline.fit(X, y)
    
    joblib.dump(pipeline, MODEL_PATH)
    msg = f"Model successfully trained on {approved_count} approved and {rejected_count} rejected vacancies."
    logger.info(msg)
    return msg

def predict_job(title, description):
    """
    Predicts the probability of a vacancy being approved.
    Returns the probability (float from 0.0 to 1.0) or None if the model is not trained.
    """
    if not os.path.exists(MODEL_PATH):
        return None

    try:
        pipeline = joblib.load(MODEL_PATH)
    except Exception as e:
        logger.error(f"Error loading ML model: {e}")
        return None

    text = f"{title} {description}".strip()
    
    # predict_proba returns an array of probabilities for each class (0 and 1)
    # We are interested in the probability of class 1 (Approved)
    probabilities = pipeline.predict_proba([text])[0]
    
    # probabilities[1] is the probability of class 1 (Approved)
    return float(probabilities[1])
