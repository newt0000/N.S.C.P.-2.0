#!/bin/bash

# Exit if any command fails
set -e

# Name of the virtual environment folder
VENV_DIR=".venv"


# Check if venv exists, create if not
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv $VENV_DIR
fi

# Activate venv
echo "Activating virtual environment..."
source $VENV_DIR/bin/activate

# Upgrade pip
pip install --upgrade pip

echo "installing dependancies..."
pip install -r requirements.txt
echo "✅ Dependencies installed."


python3 -c "import mcrcon; print('Mcrcon loaded OK')"

# Run bot
echo "🚀 Launching NSCP 2.1 BETA ..."
python3 app.py
