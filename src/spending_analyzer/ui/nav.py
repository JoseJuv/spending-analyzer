"""Page objects, in one place so pages can link to each other (import this lazily
inside a page's render function to avoid a circular import)."""

import streamlit as st

from spending_analyzer.ui import (categorize_page, chat_page, privacy_page, results_page,
                                  review_page, upload_page)

UPLOAD = st.Page(upload_page.render, title="1. Upload", icon="📄", url_path="upload",
                 default=True)
CATEGORIZE = st.Page(categorize_page.render, title="2. Categorize", icon="🏷️",
                     url_path="categorize")
REVIEW = st.Page(review_page.render, title="3. Review", icon="✅", url_path="review")
RESULTS = st.Page(results_page.render, title="4. Results", icon="📊", url_path="results")
CHAT = st.Page(chat_page.render, title="5. Ask", icon="💬", url_path="ask")
PRIVACY = st.Page(privacy_page.render, title="Data & privacy", icon="🔒", url_path="privacy")
PAGES = [UPLOAD, CATEGORIZE, REVIEW, RESULTS, CHAT, PRIVACY]
