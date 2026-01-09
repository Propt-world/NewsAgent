def to_arabic_numerals(n: int) -> str:
    """Konverts an integer to a string with Arabic numerals."""
    # Mapping from Western Arabic numerals to Eastern Arabic numerals
    english_to_arabic = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
    return str(n).translate(english_to_arabic)
