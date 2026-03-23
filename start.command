#!/bin/bash
# PostIQ — Double-click to start the web app
cd "$(dirname "$0")"
echo "============================================"
echo "  PostIQ — Credit Card Payment Posting"
echo "============================================"
echo ""
echo "  From this Mac:    http://localhost:8501"
echo "  From other PCs:   http://$(ipconfig getifaddr en0 2>/dev/null || echo 'YOUR_IP'):8501"
echo ""
echo "  Press Ctrl+C to stop."
echo ""
/Users/travmegsam/Library/Python/3.9/bin/streamlit run scripts/app.py --server.address 0.0.0.0 --server.port 8501
