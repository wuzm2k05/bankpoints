"""Unit tests for hello module"""

import unittest
import sys
import os

# Add parent directory to path to import hello module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ishopping import hello, add


class TestHello(unittest.TestCase):
    """Test cases for hello function"""
    
    def test_hello_default(self):
        """Test hello with default parameter"""
        result = hello()
        self.assertEqual(result, "Hello, World!")
    
    def test_hello_with_name(self):
        """Test hello with custom name"""
        result = hello("Alice")
        self.assertEqual(result, "Hello, Alice!")


class TestAdd(unittest.TestCase):
    """Test cases for add function"""
    
    def test_add_positive(self):
        """Test adding positive numbers"""
        result = add(2, 3)
        self.assertEqual(result, 5)
    
    def test_add_negative(self):
        """Test adding negative numbers"""
        result = add(-2, -3)
        self.assertEqual(result, -5)
    
    def test_add_mixed(self):
        """Test adding mixed positive and negative numbers"""
        result = add(10, -5)
        self.assertEqual(result, 5)


if __name__ == "__main__":
    unittest.main()
