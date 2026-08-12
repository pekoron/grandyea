"""GrandYea — lightweight Grandstream/Yealink phonebook editor.

The application intentionally uses only the Python standard library so that it
can be packaged into a small single-file executable for old Windows systems.
"""

from __future__ import print_function

import configparser
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk


APP_NAME = "GrandYea"
APP_VERSION = "1.0.3"
WORK_GROUP_NAME = "Work"


class PhonebookError(Exception):
    """A user-facing phonebook error."""


class ValidationError(PhonebookError):
    """The in-memory phonebook contains invalid data."""


class ConcurrentModificationError(PhonebookError):
    """One of the source files changed after it was loaded."""


@dataclass
class Settings:
    grandstream_path: str = ""
    yealink_path: str = ""


@dataclass
class Contact:
    key: str
    xml_id: str
    last_name: str
    first_name: str
    patronymic: str
    phone: str
    group_id: str
    group_name: str
    element: ET.Element


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return application_dir() / APP_NAME


def default_settings() -> Settings:
    base = application_dir()
    return Settings(str(base / "grandstream.xml"), str(base / "yealink.xml"))


def load_settings(path: Path) -> Settings:
    defaults = default_settings()
    parser = configparser.ConfigParser()
    try:
        if path.is_file():
            parser.read(str(path), encoding="utf-8")
        return Settings(
            parser.get("phonebooks", "grandstream", fallback=defaults.grandstream_path),
            parser.get("phonebooks", "yealink", fallback=defaults.yealink_path),
        )
    except (OSError, configparser.Error) as exc:
        logging.getLogger(APP_NAME).warning("Cannot read settings: %s", exc)
        return defaults


def save_settings(path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    parser["phonebooks"] = {
        "grandstream": settings.grandstream_path,
        "yealink": settings.yealink_path,
    }
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as stream:
            parser.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp_path), str(path))
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        directory = user_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(directory / "grandyea.log"),
            maxBytes=512 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


LOGGER = configure_logging()


def clean_text(value: Optional[str]) -> str:
    return " ".join((value or "").strip().split())


def split_grandstream_first_name(value: str) -> Tuple[str, str]:
    """Split the supported 'FirstName Patronymic' representation.

    Service names containing three or more words remain intact as a first name,
    which avoids mangling existing entries such as "Здесь никого нет".
    """

    parts = clean_text(value).split()
    if len(parts) == 2:
        return parts[0], parts[1]
    return clean_text(value), ""


def grandstream_first_name(first_name: str, patronymic: str) -> str:
    return clean_text("{} {}".format(first_name, patronymic))


def initial(value: str) -> str:
    value = clean_text(value)
    return (value[0].upper() + ".") if value else ""


def yealink_name(last_name: str, first_name: str, patronymic: str) -> str:
    last_name = clean_text(last_name)
    initials = initial(first_name) + initial(patronymic)
    if last_name and initials:
        return "{} {}".format(last_name, initials)
    return last_name or initials


def file_fingerprint(path: Path) -> Optional[Tuple[int, int, str]]:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    stat = path.stat()
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000))
    return mtime_ns, stat.st_size, digest.hexdigest()


def _get_text(element: ET.Element, path: str) -> str:
    child = element.find(path)
    return clean_text(child.text if child is not None else "")


def _insert_child_in_order(
    parent: ET.Element, tag: str, order: Tuple[str, ...]
) -> ET.Element:
    child = ET.Element(tag)
    desired = order.index(tag)
    children = list(parent)
    for index, existing in enumerate(children):
        try:
            existing_order = order.index(existing.tag)
        except ValueError:
            continue
        if existing_order > desired:
            parent.insert(index, child)
            return child
    parent.append(child)
    return child


def _set_contact_text(contact: ET.Element, tag: str, value: str) -> ET.Element:
    order = (
        "id",
        "FirstName",
        "LastName",
        "Frequent",
        "Phone",
        "Department",
        "Group",
        "Primary",
    )
    child = contact.find(tag)
    if child is None:
        child = _insert_child_in_order(contact, tag, order)
    child.text = value
    return child


def _indent(element: ET.Element, level: int = 0) -> None:
    """Python 3.8-compatible equivalent of ElementTree.indent."""

    spacing = "\n" + level * "\t"
    children = list(element)
    if children:
        if not element.text or not element.text.strip():
            element.text = spacing + "\t"
        for child in children:
            _indent(child, level + 1)
        if not children[-1].tail or not children[-1].tail.strip():
            children[-1].tail = spacing
    if level and (not element.tail or not element.tail.strip()):
        element.tail = spacing


class GrandstreamBook:
    def __init__(self, path: Path, tree: ET.ElementTree) -> None:
        self.path = path
        self.tree = tree
        self.root = tree.getroot()
        self.groups: Dict[str, str] = {}
        self.contacts: List[Contact] = []
        self._read()

    @classmethod
    def load(cls, path: Path) -> "GrandstreamBook":
        if not path.is_file():
            raise PhonebookError("Файл Grandstream не найден:\n{}".format(path))
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            tree = ET.parse(str(path), parser=parser)
        except (OSError, ET.ParseError, UnicodeError) as exc:
            raise PhonebookError(
                "Не удалось прочитать XML Grandstream:\n{}\n\n{}".format(path, exc)
            )
        if tree.getroot().tag != "AddressBook":
            raise PhonebookError(
                "Файл Grandstream имеет неизвестный корневой элемент: {}".format(
                    tree.getroot().tag
                )
            )
        return cls(path, tree)

    def _read(self) -> None:
        self.groups.clear()
        self.contacts.clear()
        for group in self.root.findall("pbgroup"):
            group_id = _get_text(group, "id")
            if group_id:
                self.groups[group_id] = _get_text(group, "name") or group_id

        for element in self.root.findall("Contact"):
            combined_first = _get_text(element, "FirstName")
            first_name, patronymic = split_grandstream_first_name(combined_first)
            group_id = _get_text(element, "Group")
            self.contacts.append(
                Contact(
                    key=uuid.uuid4().hex,
                    xml_id=_get_text(element, "id"),
                    last_name=_get_text(element, "LastName"),
                    first_name=first_name,
                    patronymic=patronymic,
                    phone=_get_text(element, "Phone/phonenumber"),
                    group_id=group_id,
                    group_name=self.groups.get(group_id, group_id or "Без группы"),
                    element=element,
                )
            )

    def contact_by_key(self, key: str) -> Contact:
        for contact in self.contacts:
            if contact.key == key:
                return contact
        raise KeyError(key)

    def _next_numeric_id(self, values: List[str], minimum: int = 1) -> str:
        numeric = [int(value) for value in values if value.isdigit()]
        candidate = max(numeric + [minimum - 1]) + 1
        used = set(values)
        while str(candidate) in used:
            candidate += 1
        return str(candidate)

    def ensure_work_group(self) -> Tuple[str, str]:
        for group_id, name in self.groups.items():
            if name.casefold() == WORK_GROUP_NAME.casefold():
                return group_id, name

        group_id = self._next_numeric_id(list(self.groups.keys()))
        group = ET.Element("pbgroup")
        ET.SubElement(group, "id").text = group_id
        ET.SubElement(group, "name").text = WORK_GROUP_NAME
        insert_at = len(list(self.root))
        for index, child in enumerate(list(self.root)):
            if child.tag == "Contact":
                insert_at = index
                break
        self.root.insert(insert_at, group)
        self.groups[group_id] = WORK_GROUP_NAME
        return group_id, WORK_GROUP_NAME

    def add_contact(
        self, last_name: str, first_name: str, patronymic: str, phone: str
    ) -> Contact:
        self.validate_values(last_name, first_name, patronymic, phone)
        self.ensure_unique_phone(phone)
        group_id, group_name = self.ensure_work_group()
        xml_id = self._next_numeric_id([contact.xml_id for contact in self.contacts])

        element = ET.Element("Contact")
        ET.SubElement(element, "id").text = xml_id
        ET.SubElement(element, "FirstName").text = grandstream_first_name(
            first_name, patronymic
        )
        ET.SubElement(element, "LastName").text = clean_text(last_name)
        ET.SubElement(element, "Frequent").text = "0"
        phone_element = ET.SubElement(element, "Phone", {"type": "Work"})
        ET.SubElement(phone_element, "phonenumber").text = clean_text(phone)
        ET.SubElement(phone_element, "accountindex").text = "1"
        ET.SubElement(element, "Group").text = group_id
        ET.SubElement(element, "Primary").text = "0"
        self.root.append(element)

        contact = Contact(
            key=uuid.uuid4().hex,
            xml_id=xml_id,
            last_name=clean_text(last_name),
            first_name=clean_text(first_name),
            patronymic=clean_text(patronymic),
            phone=clean_text(phone),
            group_id=group_id,
            group_name=group_name,
            element=element,
        )
        self.contacts.append(contact)
        return contact

    def update_contact(
        self,
        contact: Contact,
        last_name: str,
        first_name: str,
        patronymic: str,
        phone: str,
    ) -> None:
        self.validate_values(last_name, first_name, patronymic, phone)
        self.ensure_unique_phone(phone, excluding=contact)
        contact.last_name = clean_text(last_name)
        contact.first_name = clean_text(first_name)
        contact.patronymic = clean_text(patronymic)
        contact.phone = clean_text(phone)

        _set_contact_text(contact.element, "FirstName", grandstream_first_name(
            contact.first_name, contact.patronymic
        ))
        _set_contact_text(contact.element, "LastName", contact.last_name)
        phone_element = contact.element.find("Phone")
        if phone_element is None:
            phone_element = _insert_child_in_order(
                contact.element,
                "Phone",
                (
                    "id",
                    "FirstName",
                    "LastName",
                    "Frequent",
                    "Phone",
                    "Department",
                    "Group",
                    "Primary",
                ),
            )
            phone_element.set("type", "Work")
        number_element = phone_element.find("phonenumber")
        if number_element is None:
            number_element = ET.SubElement(phone_element, "phonenumber")
        number_element.text = contact.phone
        if phone_element.find("accountindex") is None:
            ET.SubElement(phone_element, "accountindex").text = "1"

    def remove_contact(self, contact: Contact) -> None:
        self.root.remove(contact.element)
        self.contacts.remove(contact)

    @staticmethod
    def validate_values(
        last_name: str, first_name: str, patronymic: str, phone: str
    ) -> None:
        del first_name, patronymic
        if not clean_text(last_name):
            raise ValidationError("Введите фамилию или название контакта.")
        phone = clean_text(phone)
        if not phone:
            raise ValidationError("Введите номер телефона.")
        if not phone.isdigit():
            raise ValidationError("Номер телефона должен содержать только цифры.")

    def ensure_unique_phone(
        self, phone: str, excluding: Optional[Contact] = None
    ) -> None:
        phone = clean_text(phone)
        for contact in self.contacts:
            if contact is not excluding and contact.phone == phone:
                raise ValidationError(
                    "Номер {} уже используется контактом «{}».".format(
                        phone, contact.last_name
                    )
                )

    def validate_all(self) -> None:
        used: Dict[str, Contact] = {}
        for contact in self.contacts:
            self.validate_values(
                contact.last_name,
                contact.first_name,
                contact.patronymic,
                contact.phone,
            )
            if contact.phone in used:
                raise ValidationError(
                    "Номер {} повторяется у контактов «{}» и «{}».".format(
                        contact.phone,
                        used[contact.phone].last_name,
                        contact.last_name,
                    )
                )
            used[contact.phone] = contact

    def grandstream_xml(self) -> bytes:
        self.validate_all()
        _indent(self.root)
        return ET.tostring(
            self.root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=False,
        ) + b"\n"

    def yealink_xml(self) -> bytes:
        self.validate_all()
        root = ET.Element("YealinkIPPhoneDirectory")
        ordered = sorted(
            self.contacts,
            key=lambda item: (
                item.last_name.casefold(),
                item.first_name.casefold(),
                item.patronymic.casefold(),
                item.phone,
            ),
        )
        for contact in ordered:
            entry = ET.SubElement(root, "DirectoryEntry")
            ET.SubElement(entry, "Name").text = yealink_name(
                contact.last_name, contact.first_name, contact.patronymic
            )
            ET.SubElement(entry, "Telephone").text = contact.phone
        _indent(root)
        return ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=False,
        ) + b"\n"


def _write_staged(path: Path, data: bytes) -> Path:
    if not path.parent.is_dir():
        raise PhonebookError("Папка не существует:\n{}".format(path.parent))
    descriptor, name = tempfile.mkstemp(
        prefix=".grandyea_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(name)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def save_pair(
    grandstream_path: Path,
    grandstream_data: bytes,
    yealink_path: Path,
    yealink_data: bytes,
) -> None:
    """Safely replace both books without leaving persistent backup files."""

    try:
        ET.fromstring(grandstream_data)
        ET.fromstring(yealink_data)
    except ET.ParseError as exc:
        raise PhonebookError("Внутренняя ошибка формирования XML: {}".format(exc))

    try:
        if grandstream_path.resolve() == yealink_path.resolve():
            raise PhonebookError("Для двух телефонных книг указаны одинаковые пути.")
    except OSError:
        pass

    staged: List[Path] = []
    destinations = [grandstream_path, yealink_path]
    data_items = [grandstream_data, yealink_data]
    originals: List[Optional[bytes]] = []
    installed = [False, False]

    try:
        for path in destinations:
            originals.append(path.read_bytes() if path.exists() else None)
        for path, data in zip(destinations, data_items):
            staged.append(_write_staged(path, data))
        for index, path in enumerate(destinations):
            os.replace(str(staged[index]), str(path))
            installed[index] = True
        return None
    except PhonebookError:
        raise
    except Exception as exc:
        rollback_errors = []
        for index, path in enumerate(destinations):
            if not installed[index]:
                continue
            try:
                if originals[index] is not None:
                    rollback_file = _write_staged(path, originals[index])
                    try:
                        os.replace(str(rollback_file), str(path))
                    finally:
                        if rollback_file.exists():
                            rollback_file.unlink()
                elif path.exists():
                    path.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        detail = ""
        if rollback_errors:
            detail = "\nОшибка отката: {}".format("; ".join(rollback_errors))
        raise PhonebookError(
            "Не удалось сохранить телефонные книги. Исходные файлы восстановлены."
            "\n{}{}".format(exc, detail)
        )
    finally:
        for path in staged:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: "PhonebookApp", settings: Settings) -> None:
        tk.Toplevel.__init__(self, parent)
        self.parent = parent
        self.title("Настройки")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Optional[Settings] = None
        self.grandstream_var = tk.StringVar(value=settings.grandstream_path)
        self.yealink_var = tk.StringVar(value=settings.yealink_path)

        frame = ttk.Frame(self, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Файл Grandstream:").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Entry(frame, textvariable=self.grandstream_var, width=62).grid(
            row=0, column=1, sticky="ew", padx=8, pady=(0, 8)
        )
        ttk.Button(
            frame, text="Обзор…", command=lambda: self._browse(self.grandstream_var)
        ).grid(row=0, column=2, pady=(0, 8))

        ttk.Label(frame, text="Файл Yealink:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.yealink_var, width=62).grid(
            row=1, column=1, sticky="ew", padx=8
        )
        ttk.Button(
            frame, text="Обзор…", command=lambda: self._browse(self.yealink_var)
        ).grid(row=1, column=2)

        ttk.Label(
            frame,
            text="Grandstream — основная книга.",
            foreground="#555555",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 16))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(buttons, text="Применить", command=self._accept).pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._accept())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self.minsize(700, self.winfo_reqheight())
        self.geometry(
            "+{}+{}".format(parent.winfo_rootx() + 80, parent.winfo_rooty() + 80)
        )

    def _browse(self, variable: tk.StringVar) -> None:
        current = clean_text(variable.get())
        initial_dir = str(Path(current).parent) if current else str(application_dir())
        value = filedialog.askopenfilename(
            parent=self,
            title="Выберите XML-файл",
            initialdir=initial_dir,
            filetypes=(("XML-файлы", "*.xml"), ("Все файлы", "*.*")),
        )
        if value:
            variable.set(value)

    def _accept(self) -> None:
        grandstream_value = self.grandstream_var.get().strip()
        yealink_value = self.yealink_var.get().strip()
        if not grandstream_value or not yealink_value:
            messagebox.showwarning(
                "Настройки", "Укажите пути к обеим телефонным книгам.", parent=self
            )
            return
        grandstream = os.path.abspath(os.path.expandvars(grandstream_value))
        yealink = os.path.abspath(os.path.expandvars(yealink_value))
        grandstream_path = Path(grandstream)
        yealink_path = Path(yealink)
        if not grandstream_path.is_file():
            messagebox.showwarning(
                "Настройки",
                "Файл Grandstream не найден:\n{}".format(grandstream_path),
                parent=self,
            )
            return
        if not yealink_path.parent.is_dir():
            messagebox.showwarning(
                "Настройки",
                "Папка файла Yealink не найдена:\n{}".format(yealink_path.parent),
                parent=self,
            )
            return
        try:
            if grandstream_path.resolve() == yealink_path.resolve():
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                "Настройки", "Для книг должны быть указаны разные файлы.", parent=self
            )
            return
        self.result = Settings(str(grandstream_path), str(yealink_path))
        self.destroy()


class PhonebookApp(tk.Tk):
    def __init__(self) -> None:
        tk.Tk.__init__(self)
        self.settings_path = user_data_dir() / "settings.ini"
        self.settings = load_settings(self.settings_path)
        self.book: Optional[GrandstreamBook] = None
        self.fingerprints: Dict[str, Optional[Tuple[int, int, str]]] = {}
        self.dirty = False
        self.sort_column = "last_name"
        self.sort_reverse = False

        self.title(APP_NAME)
        self.geometry("980x650")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._configure_style()
        self._build_ui()
        self.after(80, self._initial_load)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "xpnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        # A plain Tcl option such as "Segoe UI 9" is parsed as three list
        # items, so Tk treats "UI" as the numeric font size. Configure Tk's
        # named fonts through tkinter instead; this also handles family names
        # containing spaces correctly on every supported Tk 8.6 build.
        for font_name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(font_name).configure(family="Segoe UI", size=9)
            except tk.TclError:
                pass
        style.configure("Treeview", rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 10, 10, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Сохранить", command=self._save).pack(side="left")
        self.save_message_var = tk.StringVar()
        self.save_message_label = ttk.Label(
            toolbar,
            textvariable=self.save_message_var,
            foreground="#287a28",
            width=10,
            anchor="w",
        )
        self.save_message_label.pack(side="left", padx=(8, 0))
        self.reload_button = ttk.Button(
            toolbar, text="Перечитать", command=self._reload
        )
        self.reload_button.pack(side="left", padx=(14, 0))
        ttk.Button(toolbar, text="Настройки", command=self._open_settings).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(toolbar, text="Поиск:").pack(side="left", padx=(24, 6))
        self.search_var = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.search_var, width=28)
        search.pack(side="left", fill="x", expand=True)
        self.search_var.trace_add("write", lambda *_args: self._refresh_tree())

        main = ttk.Panedwindow(self, orient="vertical")
        main.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        table_frame = ttk.Frame(main)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("last_name", "first_name", "patronymic", "phone", "group")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "last_name": "Фамилия / название",
            "first_name": "Имя",
            "patronymic": "Отчество",
            "phone": "Номер",
            "group": "Группа",
        }
        widths = {
            "last_name": 250,
            "first_name": 170,
            "patronymic": 180,
            "phone": 110,
            "group": 120,
        }
        for column in columns:
            self.tree.heading(
                column,
                text=headings[column],
                command=lambda value=column: self._sort_by(value),
            )
            self.tree.column(column, width=widths[column], minwidth=80)
        self.tree.column("phone", anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", lambda _event: self.first_entry.focus_set())
        main.add(table_frame, weight=4)

        editor = ttk.LabelFrame(main, text="Контакт", padding=10)
        for index in range(5):
            editor.columnconfigure(index, weight=1 if index < 4 else 0)
        self.last_name_var = tk.StringVar()
        self.first_name_var = tk.StringVar()
        self.patronymic_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.group_var = tk.StringVar(value="Work (для нового контакта)")
        fields = (
            ("Фамилия / название", self.last_name_var),
            ("Имя", self.first_name_var),
            ("Отчество", self.patronymic_var),
            ("Номер", self.phone_var),
        )
        entries = []
        for index, (label, variable) in enumerate(fields):
            ttk.Label(editor, text=label + ":").grid(
                row=0, column=index, sticky="w", padx=(0, 8)
            )
            entry = ttk.Entry(editor, textvariable=variable)
            entry.grid(row=1, column=index, sticky="ew", padx=(0, 8), pady=(3, 10))
            entries.append(entry)
        self.first_entry = entries[0]
        ttk.Label(editor, text="Группа:").grid(row=2, column=0, sticky="w")
        ttk.Label(editor, textvariable=self.group_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(3, 0)
        )
        editor_buttons = ttk.Frame(editor)
        editor_buttons.grid(row=2, column=2, columnspan=3, rowspan=2, sticky="e")
        ttk.Button(editor_buttons, text="Новый", command=self._new_contact).pack(
            side="left"
        )
        ttk.Button(
            editor_buttons, text="Добавить / применить", command=self._apply_contact
        ).pack(side="left", padx=(6, 0))
        ttk.Button(editor_buttons, text="Удалить", command=self._delete_contact).pack(
            side="left", padx=(6, 0)
        )
        for entry in entries:
            entry.bind("<Return>", lambda _event: self._apply_contact())
        main.add(editor, weight=0)

        self.status_var = tk.StringVar(value="Загрузка…")
        status = ttk.Label(
            self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(8, 4)
        )
        status.pack(fill="x", side="bottom")

    def _initial_load(self) -> None:
        if Path(self.settings.grandstream_path).is_file():
            self._load_book()
        else:
            self.status_var.set("Укажите путь к книге Grandstream в настройках")
            self._open_settings(force=True)

    def _load_book(self) -> bool:
        try:
            grandstream_path = Path(self.settings.grandstream_path)
            yealink_path = Path(self.settings.yealink_path)
            book = GrandstreamBook.load(grandstream_path)
            book.validate_all()
            self.book = book
            self.fingerprints = {
                "grandstream": file_fingerprint(grandstream_path),
                "yealink": file_fingerprint(yealink_path),
            }
            self.dirty = False
            self.save_message_var.set("")
            self._new_contact()
            self._refresh_tree()
            self._update_status("Книга Grandstream загружена")
            LOGGER.info("Loaded %s contacts from %s", len(book.contacts), grandstream_path)
            return True
        except Exception as exc:
            LOGGER.exception("Load failed")
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            self.status_var.set("Ошибка загрузки")
            return False

    def _refresh_tree(self, select_key: Optional[str] = None) -> None:
        current = select_key or self._selected_key()
        self.tree.delete(*self.tree.get_children())
        if self.book is None:
            return
        query = clean_text(self.search_var.get()).casefold()
        contacts = list(self.book.contacts)

        def sort_value(contact: Contact) -> str:
            values = {
                "last_name": contact.last_name,
                "first_name": contact.first_name,
                "patronymic": contact.patronymic,
                "phone": contact.phone.zfill(30),
                "group": contact.group_name,
            }
            return values.get(self.sort_column, contact.last_name).casefold()

        contacts.sort(key=sort_value, reverse=self.sort_reverse)
        for contact in contacts:
            haystack = " ".join(
                (
                    contact.last_name,
                    contact.first_name,
                    contact.patronymic,
                    contact.phone,
                    contact.group_name,
                )
            ).casefold()
            if query and query not in haystack:
                continue
            self.tree.insert(
                "",
                "end",
                iid=contact.key,
                values=(
                    contact.last_name,
                    contact.first_name,
                    contact.patronymic,
                    contact.phone,
                    contact.group_name,
                ),
            )
        if current and self.tree.exists(current):
            self.tree.selection_set(current)
            self.tree.focus(current)
            self.tree.see(current)
        self._update_title()

    def _sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._refresh_tree()

    def _selected_key(self) -> Optional[str]:
        selected = self.tree.selection()
        return selected[0] if selected else None

    def _selected_contact(self) -> Optional[Contact]:
        key = self._selected_key()
        if self.book is None or not key:
            return None
        try:
            return self.book.contact_by_key(key)
        except KeyError:
            return None

    def _selection_changed(self, _event: object = None) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        self.last_name_var.set(contact.last_name)
        self.first_name_var.set(contact.first_name)
        self.patronymic_var.set(contact.patronymic)
        self.phone_var.set(contact.phone)
        self.group_var.set(contact.group_name)

    def _new_contact(self) -> None:
        if self.tree.selection():
            self.tree.selection_remove(*self.tree.selection())
        self.last_name_var.set("")
        self.first_name_var.set("")
        self.patronymic_var.set("")
        self.phone_var.set("")
        self.group_var.set("Work (для нового контакта)")
        self.first_entry.focus_set()

    def _apply_contact(self) -> None:
        if self.book is None:
            messagebox.showwarning(APP_NAME, "Сначала загрузите телефонную книгу.")
            return
        try:
            contact = self._selected_contact()
            values = (
                self.last_name_var.get(),
                self.first_name_var.get(),
                self.patronymic_var.get(),
                self.phone_var.get(),
            )
            if contact is None:
                contact = self.book.add_contact(*values)
                action = "Контакт добавлен в группу Work"
            else:
                self.book.update_contact(contact, *values)
                action = "Изменения применены"
            self.dirty = True
            self.save_message_var.set("")
            self._refresh_tree(select_key=contact.key)
            self._update_status(action)
        except ValidationError as exc:
            messagebox.showwarning("Проверка данных", str(exc), parent=self)

    def _delete_contact(self) -> None:
        if self.book is None:
            return
        contact = self._selected_contact()
        if contact is None:
            messagebox.showinfo(APP_NAME, "Выберите контакт для удаления.", parent=self)
            return
        if not messagebox.askyesno(
            "Удаление",
            "Удалить контакт «{}» с номером {}?".format(
                contact.last_name, contact.phone
            ),
            parent=self,
        ):
            return
        self.book.remove_contact(contact)
        self.dirty = True
        self.save_message_var.set("")
        self._new_contact()
        self._refresh_tree()
        self._update_status("Контакт удалён")

    def _files_unchanged(self) -> bool:
        grandstream = file_fingerprint(Path(self.settings.grandstream_path))
        yealink = file_fingerprint(Path(self.settings.yealink_path))
        return (
            grandstream == self.fingerprints.get("grandstream")
            and yealink == self.fingerprints.get("yealink")
        )

    def _save(self) -> None:
        if self.book is None:
            messagebox.showwarning(APP_NAME, "Сначала загрузите телефонную книгу.")
            return
        try:
            if not self._files_unchanged():
                raise ConcurrentModificationError(
                    "Один из XML-файлов изменился после загрузки.\n\n"
                    "Чтобы не потерять чужие изменения, перечитайте книги и повторите правку."
                )
            grandstream_path = Path(self.settings.grandstream_path)
            yealink_path = Path(self.settings.yealink_path)
            grandstream_data = self.book.grandstream_xml()
            yealink_data = self.book.yealink_xml()
            save_pair(
                grandstream_path,
                grandstream_data,
                yealink_path,
                yealink_data,
            )
            self.fingerprints = {
                "grandstream": file_fingerprint(grandstream_path),
                "yealink": file_fingerprint(yealink_path),
            }
            self.dirty = False
            self.save_message_var.set("Сохранено")
            self._update_status("Обе телефонные книги сохранены")
            LOGGER.info("Saved both phonebooks")
        except Exception as exc:
            LOGGER.exception("Save failed")
            messagebox.showerror("Ошибка сохранения", str(exc), parent=self)

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        return messagebox.askyesno(
            "Несохранённые изменения",
            "Отменить несохранённые изменения?",
            parent=self,
        )

    def _reload(self) -> None:
        if self._confirm_discard():
            self._load_book()

    def _open_settings(self, force: bool = False) -> None:
        if not force and not self._confirm_discard():
            return
        dialog = SettingsDialog(self, self.settings)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        try:
            save_settings(self.settings_path, dialog.result)
            self.settings = dialog.result
            self._load_book()
        except Exception as exc:
            LOGGER.exception("Cannot save settings")
            messagebox.showerror("Настройки", str(exc), parent=self)

    def _update_status(self, message: str) -> None:
        count = len(self.book.contacts) if self.book is not None else 0
        suffix = " | есть несохранённые изменения" if self.dirty else ""
        self.status_var.set("{} | контактов: {}{}".format(message, count, suffix))
        self._update_title()

    def _update_title(self) -> None:
        marker = " *" if self.dirty else ""
        self.title("{} {}{}".format(APP_NAME, APP_VERSION, marker))

    def _close(self) -> None:
        if self._confirm_discard():
            self.destroy()


def main() -> int:
    try:
        app = PhonebookApp()
        app.mainloop()
        return 0
    except Exception as exc:
        LOGGER.exception("Fatal application error")
        try:
            messagebox.showerror(APP_NAME, "Критическая ошибка:\n{}".format(exc))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
