from setuptools import setup, find_packages

setup(
    name="helix",
    version="0.1.0",
    description="Helix - AI-Powered Package Manager for Linux",
    packages=find_packages(),
    include_package_data=True,
    package_data={"helix": ["stacks.json"]},
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "helix=helix.cli:main",
        ],
    },
    install_requires=[
        "anthropic>=0.18.0",
        "openai>=1.0.0",
        "requests>=2.32.4",
        "python-dotenv>=1.0.0",
        "cryptography>=42.0.0",
        "rich>=13.0.0",
        "typing-extensions>=4.0.0",
    ],
)