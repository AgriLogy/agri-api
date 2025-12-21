#!/bin/bash

python manage.py makemigrations
python manage.py migrate
echo "Starting Django server..."

python dummy.py

python manage.py runserver 0.0.0.0:8000