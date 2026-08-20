from server import app, ensure_runtime_started


def main() -> None:
    ensure_runtime_started()
    app.run(host="0.0.0.0", port=8000, debug=False)


if __name__ == "__main__":
    main()
