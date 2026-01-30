"""Tests for EditFileFunctionExecutor using yaml definitions."""

import pytest

from custom_components.extended_openai_conversation.helpers import (
    EditFileFunctionExecutor,
    get_function_executor,
)
from tests.helpers import get_function_from_yaml


class TestEditFileFunctionExecutorYaml:
    """Test EditFileFunctionExecutor using yaml definitions."""

    @pytest.fixture
    def executor(self):
        """Create EditFileFunctionExecutor instance."""
        return EditFileFunctionExecutor()

    async def test_edit_file_success_from_yaml(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing a file successfully from yaml definition."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "edit_test.txt"
        test_file.write_text("Hello World\nThis is a test.")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "edit_test.txt",
            "old_text": "Hello World",
            "new_text": "Goodbye World",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert result["success"] is True
        assert "path" in result
        assert result["replacements"] == 1

        new_content = test_file.read_text()
        assert "Goodbye World" in new_content
        assert "Hello World" not in new_content
        assert "This is a test." in new_content

    async def test_edit_file_multiline_replacement_from_yaml(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing multiple lines from yaml."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "multiline.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "multiline.txt",
            "old_text": "Line 2\nLine 3",
            "new_text": "New Line 2\nNew Line 3",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert result["success"] is True
        new_content = test_file.read_text()
        assert "New Line 2\nNew Line 3" in new_content
        assert "Line 2\nLine 3" not in new_content

    async def test_edit_file_not_found(
        self, hass, executor, exposed_entities, llm_context
    ):
        """Test editing a file that doesn't exist."""
        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "nonexistent.txt",
            "old_text": "old",
            "new_text": "new",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_edit_file_text_not_found(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing when old_text doesn't exist in file."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "test.txt"
        test_file.write_text("Some content without the searched text")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "test.txt",
            "old_text": "Not in file",
            "new_text": "new text",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert "error" in result
        assert "not found in file" in result["error"].lower()

    async def test_edit_file_multiple_occurrences(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test that multiple occurrences are rejected."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "duplicate.txt"
        test_file.write_text("Hello\nHello\nGoodbye")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "duplicate.txt",
            "old_text": "Hello",
            "new_text": "Hi",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert "error" in result
        assert "2 times" in result["error"]
        assert test_file.read_text() == "Hello\nHello\nGoodbye"

    async def test_edit_file_is_directory(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing a directory instead of file."""
        workdir = tmp_path / "extended_openai_conversation"
        test_dir = workdir / "testdir"
        test_dir.mkdir()

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "testdir",
            "old_text": "old",
            "new_text": "new",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert "error" in result
        assert "not a file" in result["error"].lower()

    async def test_edit_file_outside_allowed_dir(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test that editing files outside allowed directories is blocked."""
        external_file = tmp_path / "external.txt"
        external_file.write_text("External content")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": str(external_file),
            "old_text": "External",
            "new_text": "Modified",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert "error" in result
        assert ("access denied" in result["error"].lower() or
                "not in allowed" in result["error"].lower())
        assert external_file.read_text() == "External content"

    async def test_edit_file_with_custom_allow_dir_from_yaml(
        self, hass, executor, exposed_entities, llm_context
    ):
        """Test editing file in custom allowed directory from yaml."""
        import os

        # Use /tmp directory as specified in yaml
        test_file_path = os.path.join("/tmp", "custom_edit_test.txt")

        with open(test_file_path, "w") as f:
            f.write("Original text")

        try:
            func_def = get_function_from_yaml("edit_file_example.yaml", 1)
            function_executor = get_function_executor(func_def["function"]["type"])
            processed_function = function_executor.to_arguments(func_def["function"])

            arguments = {
                "filepath": test_file_path,
                "old_text": "Original",
                "new_text": "Modified",
            }

            result = await executor.execute(
                hass, processed_function, arguments, llm_context, exposed_entities
            )

            assert result["success"] is True
            with open(test_file_path) as f:
                assert f.read() == "Modified text"
        finally:
            # Clean up
            if os.path.exists(test_file_path):
                os.remove(test_file_path)

    async def test_edit_file_template_path(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing file with templated path."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "templated.txt"
        test_file.write_text("Template test")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "templated.txt",
            "old_text": "Template",
            "new_text": "Replaced",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert result["success"] is True
        assert test_file.read_text() == "Replaced test"

    async def test_edit_file_unicode_content_from_yaml(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test editing file with unicode content from yaml."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "unicode.txt"
        test_file.write_text("Hello 世界", encoding="utf-8")

        func_def = get_function_from_yaml("edit_file_example.yaml", 2)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "unicode.txt",
            "old_text": "世界",
            "new_text": "World",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert result["success"] is True
        assert test_file.read_text(encoding="utf-8") == "Hello World"

    async def test_edit_file_preserves_rest_of_content(
        self, hass, executor, exposed_entities, llm_context, tmp_path
    ):
        """Test that edit preserves content before and after replacement."""
        workdir = tmp_path / "extended_openai_conversation"
        test_file = workdir / "preserve.txt"
        test_file.write_text("Start\nMiddle line to replace\nEnd")

        func_def = get_function_from_yaml("edit_file_example.yaml", 0)
        function_executor = get_function_executor(func_def["function"]["type"])
        processed_function = function_executor.to_arguments(func_def["function"])

        arguments = {
            "filename": "preserve.txt",
            "old_text": "Middle line to replace",
            "new_text": "New middle line",
        }

        result = await executor.execute(
            hass, processed_function, arguments, llm_context, exposed_entities
        )

        assert result["success"] is True
        new_content = test_file.read_text()
        assert "Start" in new_content
        assert "New middle line" in new_content
        assert "End" in new_content
        assert "Middle line to replace" not in new_content

    async def test_get_function_executor(self):
        """Test getting edit_file executor from registry."""
        executor = get_function_executor("edit_file")
        assert isinstance(executor, EditFileFunctionExecutor)
