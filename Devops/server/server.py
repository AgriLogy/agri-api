import json
import os
import socket
import threading
from datetime import datetime

# Server configuration
HOST = "0.0.0.0"
PORT = 9090
LOG_FILE = "/shared/requests.json"  # Ensure this is shared between host and containers


def ensure_log_file():
    """Ensure the log file exists and is initialized as a JSON list."""
    if not os.path.exists(LOG_FILE) or os.stat(LOG_FILE).st_size == 0:
        with open(LOG_FILE, "w") as log_file:
            json.dump([], log_file)


def extract_json(raw_data: str) -> str:
    """Extract JSON content by trimming data before '{' and after '}'."""
    start = raw_data.find("{")
    end = raw_data.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw_data[start : end + 1]
    return None


def log_raw_data(json_data: str):
    """Log JSON data as a list in the file after adding a timestamp."""
    ensure_log_file()

    try:
        data = json.loads(json_data)
        data["timestamp"] = datetime.now().isoformat()

        # Try to read existing data
        with open(LOG_FILE, "r") as log_file:
            try:
                existing_data = json.load(log_file)
            except json.JSONDecodeError:
                existing_data = []

        # Append new entry
        existing_data.append(data)

        # Write updated list back
        with open(LOG_FILE, "w") as log_file:
            json.dump(existing_data, log_file, indent=4)

    except json.JSONDecodeError:
        print("Invalid JSON format.")
    except Exception as e:
        print(f"Error writing to {LOG_FILE}: {e}")


def handle_client(client_socket):
    """Handle client connection and log JSON data."""
    try:
        request = client_socket.recv(4096).decode("utf-8")
        json_data = extract_json(request)

        if json_data:
            print(json_data)
            log_raw_data(json_data)
        else:
            print("No valid JSON found.")

        response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nRequest received"
        client_socket.sendall(response.encode("utf-8"))

    except Exception as e:
        print(f"Error handling client: {e}")

    finally:
        client_socket.close()


def start_server():
    """Start the TCP server."""
    ensure_log_file()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((HOST, PORT))
    server.listen(5)
    print(f"Server listening on {HOST}:{PORT}")

    try:
        while True:
            client_socket, addr = server.accept()
            print(f"\nNew connection from {addr}")
            threading.Thread(target=handle_client, args=(client_socket,)).start()

    except KeyboardInterrupt:
        print("\nServer shutting down.")

    finally:
        server.close()


if __name__ == "__main__":
    start_server()
