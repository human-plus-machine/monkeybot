"""
Skills execution engine.

This module provides the SkillsEngine class which executes skills via the
Terminal Executor with proper security validation.
"""

import logging
from typing import Dict, Any, List

from monkeybot.core.types.interfaces import SkillsEngineInterface, SkillResult
from monkeybot.core.tools.terminal import TerminalExecutor, SecurityError
from monkeybot.skills.loader import SkillLoader

logger = logging.getLogger(__name__)


class SkillsEngine(SkillsEngineInterface):
    """
    Execute skills via Terminal Executor.
    
    The SkillsEngine loads skills from the filesystem and executes them
    by converting skill arguments to command-line arguments and running
    them through the Terminal Executor for security validation.
    
    Attributes:
        terminal: Terminal executor for running commands
        loader: Skill loader for discovering skills
        skills: Dictionary of loaded skills
    
    Example:
        >>> terminal = TerminalExecutor()
        >>> engine = SkillsEngine(terminal)
        >>> result = await engine.execute_skill(
        ...     "file-ops",
        ...     {"action": "read", "path": "./data/memory/file.txt"}
        ... )
        >>> print(result.output)
    """
    
    def __init__(self, terminal_executor: TerminalExecutor, skills_dir: str = "./skills"):
        """
        Initialize skills engine.
        
        Args:
            terminal_executor: Terminal executor instance for running commands
            skills_dir: Path to skills directory (default: ./skills)
        """
        self.terminal = terminal_executor
        self.loader = SkillLoader(skills_dir)
        self.skills = self.loader.load_skills()
        
        logger.info(
            f"Skills engine initialized with {len(self.skills)} skills",
            extra={"component": "skills_engine", "count": len(self.skills)}
        )
    
    async def execute_skill(
        self, 
        skill_name: str, 
        args: Dict[str, Any]
    ) -> SkillResult:
        """
        Execute a skill by name with arguments.
        
        Converts the arguments dictionary to command-line arguments and executes
        the skill's Python entry point via the Terminal Executor.
        
        Args:
            skill_name: Skill identifier from SKILL.md
            args: Skill arguments from LLM tool call
            
        Returns:
            SkillResult with success status and output
        
        Notes:
            - All errors are captured and returned as SkillResult (no exceptions propagate)
            - Security violations from Terminal Executor are captured
            - Skills must handle their own argument parsing
        """
        # Check if skill exists
        if skill_name not in self.skills:
            logger.warning(
                f"Skill not found: {skill_name}",
                extra={"component": "skills_engine", "skill": skill_name}
            )
            return SkillResult(
                success=False,
                output="",
                error=f"Skill '{skill_name}' not found"
            )
        
        skill = self.skills[skill_name]
        entry_point = skill["entry_point"]
        
        # Build command arguments
        cmd_args = self._build_command_args(entry_point, args)
        
        logger.info(
            f"Executing skill: {skill_name}",
            extra={
                "component": "skills_engine",
                "skill": skill_name,
                "args": args
            }
        )
        
        try:
            # Execute via Terminal Executor
            result = await self.terminal.execute("python3", cmd_args)
            
            if result.exit_code == 0:
                return SkillResult(
                    success=True,
                    output=result.stdout.strip()
                )
            else:
                return SkillResult(
                    success=False,
                    output=result.stdout.strip(),
                    error=result.stderr.strip()
                )
        
        except SecurityError as e:
            logger.error(
                f"Security violation executing skill {skill_name}: {e}",
                extra={"component": "skills_engine", "skill": skill_name}
            )
            return SkillResult(
                success=False,
                output="",
                error=f"Security violation: {str(e)}"
            )
        
        except TimeoutError as e:
            logger.error(
                f"Timeout executing skill {skill_name}: {e}",
                extra={"component": "skills_engine", "skill": skill_name}
            )
            return SkillResult(
                success=False,
                output="",
                error=f"Skill execution timeout: {str(e)}"
            )
        
        except Exception as e:
            logger.error(
                f"Unexpected error executing skill {skill_name}: {e}",
                extra={"component": "skills_engine", "skill": skill_name, "error": str(e)}
            )
            return SkillResult(
                success=False,
                output="",
                error=f"Unexpected error: {str(e)}"
            )
    
    def list_skills(self) -> List[str]:
        """
        Return list of available skill names.
        
        Returns:
            List of skill names loaded from filesystem
        """
        return list(self.skills.keys())
    
    def _build_command_args(self, entry_point: str, args: Dict[str, Any]) -> List[str]:
        """
        Convert skill metadata + args dict to command line args.
        
        Converts a dictionary of arguments to command-line format:
            {"action": "read", "path": "file.txt"}
            -> [entry_point, "--action", "read", "--path", "file.txt"]
        
        Args:
            entry_point: Path to skill's Python file
            args: Arguments dict from LLM tool call
            
        Returns:
            List of command args ready for subprocess execution
        
        Notes:
            - Empty args dict results in [entry_point] only
            - All values are converted to strings
            - Keys are prefixed with "--"
        """
        cmd_args = [entry_point]
        
        for key, value in args.items():
            cmd_args.extend([f"--{key}", str(value)])
        
        return cmd_args
