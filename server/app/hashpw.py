"""Passwort-Hash für USERS erzeugen: python -m app.hashpw"""
import getpass
import secrets
import sys

from .auth import hash_password

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else input("Benutzername: ").strip()
    pw = getpass.getpass("Passwort: ")
    if len(pw) < 10:
        sys.exit("Passwort bitte mindestens 10 Zeichen lang wählen.")
    if pw != getpass.getpass("Wiederholen: "):
        sys.exit("Passwörter stimmen nicht überein.")
    print(f"\nUSERS={name}:{hash_password(pw)}")
    print(f"(Für SECRET_KEY z. B.: {secrets.token_urlsafe(48)})")
