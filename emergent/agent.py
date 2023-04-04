from typing import Optional, List
import traceback
import json
import re
import logging

from .llms import openai_chat_completion
from .memory import HierarchicalMemory
from .tools import example_messages


class ToolManager:

    """
    A class to manage and process tool calls from a large language model (LLM).

    Attributes
    ----------
    agent : object
        The agent instance to be managed by the ToolManager
    tools : list
        A list of tools available for the agent
    """

    def __init__(self, agent):
        self.agent = agent
        self.tools = agent.tools

    def handle_message(self, content):
        """
        Process a message from the agent. If the message contains a tool call,
        process it and return the LLM's response to the tool.
        """

        # Keep executing tools in a chain until the agent stops calling them
        # TODO: add a timeout/max tries to prevent infinite loops

        while (match := self.parse_tools(content)) is not None:
            tool, kwargs, matched_string = match
            print("FUNCTION CALL: ", tool.schema.name, kwargs)

            result = self.process_tool(tool, kwargs, matched_string)
            content = self.agent.get_response()

        self.agent.add_message(role="assistant", content=content)
        return content

    def process_tool(self, tool, kwargs, matched_string):
        """Process a tool call and return the result of the tool's execution."""
        if isinstance(kwargs, json.JSONDecodeError):
            result = "Error decoding JSON"
        else:
            result = self.call_tool(tool, kwargs)

        # Make the agent think that calling the tool worked
        self.agent.messages.append(
            dict(role="assistant", content=f"{matched_string}\n-> [{result}]")
        )
        return result

    def call_tool(self, tool, kwargs):
        """
        Call a tool with the given parameters. If the tool fails, return a
        message with the valid parameters. If the tool raises an exception,
        return the traceback.
        """
        try:
            return tool(**kwargs)
        except TypeError as e:
            valid_params = json.dumps(tool.schema.parameters, indent=4)
            return (
                f"Invalid parameters for {tool.schema.name}:\n{e},"
                f" valid parameters are:\n{valid_params}"
            )
        except Exception as e:
            return traceback.format_exc()

    def parse_tools(self, content):
        """Parse a message for tool calls and return the tool and its parameters."""
        tool_patterns = [(rf"{tool.schema.name}\((.*?)\)", tool) for tool in self.tools]

        for pattern, tool in tool_patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                tool_data = self.extract_data(match)
                return tool, tool_data, content[match.start() : match.end()]

        return None

    def extract_data(self, match):
        """Extract the parameters from a tool call."""
        try:
            return json.loads(match.group(1))
        except json.decoder.JSONDecodeError as e:
            logging.error(f"Error decoding JSON: {match.group(1)}")
            return e

    def format_tool_usage(self):
        if not self.tools:
            return "TOOLS\n-------\nCurrently you have no tools available."
        
        msg = "TOOLS\n-------\n"
        msg += "The way you can use a tool is by calling them in your messages with a JSON object as the parameter. "

        if len(self.tools) > 1:
            msg += f"You currently have access to {len(self.tools)} tools:\n\n"
        else:
            msg += "You currently have access to one tool:\n\n"
        

        for i, tool in enumerate(self.tools):
            msg += f"{i+1}. `{tool.schema.name}(json)` - {tool.schema.description}\n\n"
            msg += "Example usage:\n"
            msg += tool.schema.usage + "\n-> [results will show up here]\n\n"
        return msg

class ChatAgent:
    """A basic chatbot agent that uses OpenAI's chat completions API."""

    def __init__(
        self,
        memory: HierarchicalMemory = None,
        tools: Optional[List[callable]] = None,
        message_window=20,
        rolling_window_size=20,
        model="gpt-4",
    ):
        self.language_model = model
        self.tools = tools or []
        self.k_shot_messages = []
        self.tool_manager = ToolManager(self)
        if memory:
            self.k_shot_messages = example_messages

        self.memory = memory
        if self.memory:
            self.memory.rolling_window_size = rolling_window_size
        self.messages = []
        self.message_window = message_window
        self.system_prompt = []
        self.system_prompt = ""

        self.system_prompt = (
            "You are a friendly AI agent that has access to a variety of tools. "
            "You can use these tools to help you solve problems.\n\n"
        )
        if self.memory:
            self.system_prompt = (
                "You are an AI agent that has access to a long term memory system. "
                "You should use this tool when you are unsure about information about a person, place, idea or concept.\n\n"

            )
            if len(self.tools) > 1:
                self.system_prompt += (
                    "You also have access to a variety of other tools that you can use to help you solve problems.\n\n"
                )
        
        usage = self.tool_manager.format_tool_usage()
        self.system_prompt += usage

    @property
    def system_message(self):
        return [{"role": "system", "content": self.system_prompt}]

    def add_message(self, role: str, content: str):
        """Add a message to the agent's memory."""
        self.messages.append({"role": role, "content": content})
        if self.memory:
            self.memory.add_log(role, content)

    def get_response(self) -> str:
        """Get a response from the agent."""

        response = openai_chat_completion(
            model=self.language_model,
            messages=self.k_shot_messages + self.system_message + self.messages,
            temperature=0.2,
            stop=["->"]
        )
        print(response.choices[0].message.content)
        return response.choices[0].message.content

    def send(self, message) -> str:
        """Send a message to the agent. While also managing chat history."""
        self.add_message(role="user", content=message)
        response = self.get_response()
        return self.tool_manager.handle_message(response)

    def end_conversation(self, path):
        """If the conversation is ended, the current messages, regardless of length, are summarized and the memory is saved"""
        self.memory.build_summary_node()
        with open(path, "w") as f:
            f.write(self.memory.to_json())

    def clear_memory(self):
        self.messages = []

    def _trim_messages(self):
        """Removes the first messages, when the current length of messages is longer then the message_window"""
        while len(self.messages) > self.message_window:
            self.messages.pop(0)  # Remove the oldest message
