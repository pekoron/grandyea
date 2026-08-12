import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from grandyea import (
    GrandstreamBook,
    ValidationError,
    file_fingerprint,
    save_pair,
    split_grandstream_first_name,
    yealink_name,
)


PROJECT_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = PROJECT_DIR / "examples"


class FormattingTests(unittest.TestCase):
    def test_yealink_name_variants(self):
        self.assertEqual(yealink_name("Иванов", "", ""), "Иванов")
        self.assertEqual(yealink_name("Иванов", "Иван", ""), "Иванов И.")
        self.assertEqual(
            yealink_name("Иванов", "Иван", "Иванович"), "Иванов И.И."
        )

    def test_existing_service_name_is_not_split(self):
        self.assertEqual(
            split_grandstream_first_name("Здесь никого нет"),
            ("Здесь никого нет", ""),
        )


class PhonebookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.grandstream = self.directory / "grandstream.xml"
        self.yealink = self.directory / "yealink.xml"
        self.grandstream.write_bytes((EXAMPLES_DIR / "grandstream.xml").read_bytes())
        self.yealink.write_bytes((EXAMPLES_DIR / "yealink.xml").read_bytes())

    def tearDown(self):
        self.temporary.cleanup()

    def test_loads_utf8_sample_and_all_groups(self):
        book = GrandstreamBook.load(self.grandstream)
        self.assertEqual(len(book.contacts), 5)
        self.assertEqual(book.contacts[0].first_name, "Иван")
        self.assertEqual(book.contacts[0].patronymic, "Петрович")
        self.assertEqual(book.contacts[0].last_name, "Кузнецов")
        self.assertEqual(book.contacts[0].group_name, "Work")

    def test_add_with_patronymic_and_generate_yealink(self):
        book = GrandstreamBook.load(self.grandstream)
        added = book.add_contact("Иванов", "Иван", "Иванович", "999")
        self.assertEqual(added.group_name, "Work")

        grandstream_root = ET.fromstring(book.grandstream_xml())
        added_xml = [
            item
            for item in grandstream_root.findall("Contact")
            if item.findtext("Phone/phonenumber") == "999"
        ][0]
        self.assertEqual(added_xml.findtext("FirstName"), "Иван Иванович")
        self.assertEqual(added_xml.findtext("Group"), "6")

        yealink_root = ET.fromstring(book.yealink_xml())
        names = {
            item.findtext("Telephone"): item.findtext("Name")
            for item in yealink_root.findall("DirectoryEntry")
        }
        self.assertEqual(names["999"], "Иванов И.И.")
        self.assertEqual(len(names), 6)

    def test_update_and_delete(self):
        book = GrandstreamBook.load(self.grandstream)
        contact = book.contacts[0]
        book.update_contact(contact, "Новая", "Анна", "Петровна", "700")
        self.assertEqual(contact.element.findtext("FirstName"), "Анна Петровна")
        self.assertEqual(contact.element.findtext("Phone/phonenumber"), "700")
        book.remove_contact(contact)
        self.assertEqual(len(book.contacts), 4)

    def test_rejects_duplicate_and_non_numeric_phone(self):
        book = GrandstreamBook.load(self.grandstream)
        with self.assertRaises(ValidationError):
            book.add_contact("Тест", "", "", "100")
        with self.assertRaises(ValidationError):
            book.add_contact("Тест", "", "", "+7999")

    def test_atomic_pair_save_without_backups(self):
        book = GrandstreamBook.load(self.grandstream)
        book.add_contact("Иванов", "Иван", "", "999")
        old_grandstream = file_fingerprint(self.grandstream)
        old_yealink = file_fingerprint(self.yealink)
        save_pair(
            self.grandstream,
            book.grandstream_xml(),
            self.yealink,
            book.yealink_xml(),
        )
        self.assertEqual(list(self.directory.glob("*.bak")), [])
        self.assertNotEqual(file_fingerprint(self.grandstream), old_grandstream)
        self.assertNotEqual(file_fingerprint(self.yealink), old_yealink)
        self.assertEqual(
            len(ET.parse(str(self.yealink)).getroot().findall("DirectoryEntry")), 6
        )


if __name__ == "__main__":
    unittest.main()
