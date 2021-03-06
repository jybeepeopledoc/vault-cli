import contextlib
import logging
import os
import pathlib
import sys
from typing import (
    Any,
    Dict,
    Generator,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    TextIO,
    Tuple,
)

import click
import yaml

import vault_cli
from vault_cli import client, environment, exceptions, settings, types

logger = logging.getLogger(__name__)

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "auto_envvar_prefix": settings.ENV_PREFIX,
}


def load_config(ctx: click.Context, param: click.Parameter, value: str) -> None:
    if value == "no":
        ctx.default_map = {}
        return

    if value is None:
        config_files = settings.CONFIG_FILES
    else:
        config_files = [value]

    config = settings.build_config_from_files(*config_files)
    ctx.default_map = config


def set_verbosity(ctx: click.Context, param: click.Parameter, value: int) -> int:
    level = settings.get_log_level(verbosity=value)
    logging.basicConfig(level=level)
    logger.info(f"Log level set to {logging.getLevelName(level)}")
    return value


@contextlib.contextmanager
def handle_errors():
    try:
        yield
    except exceptions.VaultException as exc:
        raise click.ClickException(str(exc))


def print_version(ctx, __, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"vault-cli {vault_cli.__version__}")
    click.echo(f"License: {vault_cli.__license__}")
    ctx.exit()


@click.group(context_settings=CONTEXT_SETTINGS)
@click.pass_context
@click.option(
    "--url", "-U", help="URL of the vault instance", default=settings.DEFAULTS.url
)
@click.option(
    "--verify/--no-verify",
    default=settings.DEFAULTS.verify,
    help="Verify HTTPS certificate",
)
@click.option(
    "--ca-bundle",
    type=click.Path(),
    help="Location of the bundle containing the server certificate "
    "to check against.",
)
@click.option(
    "--login-cert",
    type=click.Path(),
    help="Path to a public client certificate to use for connecting to vault.",
)
@click.option(
    "--login-cert-key",
    type=click.Path(),
    help="Path to a private client certificate to use for connecting to vault.",
)
@click.option(
    "--token-file",
    "-T",
    type=click.Path(),
    help="File which contains the token to connect to Vault. "
    'Configuration file can also contain a "token" key.',
)
@click.option("--username", "-u", help="Username used for userpass authentication")
@click.option(
    "--password-file",
    "-w",
    type=click.Path(),
    help='Can read from stdin if "-" is used as parameter. '
    'Configuration file can also contain a "password" key.',
)
@click.option("--base-path", "-b", help="Base path for requests")
@click.option(
    "-s",
    "--safe-write/--unsafe-write",
    default=settings.DEFAULTS.safe_write,
    help="When activated, you can't overwrite a secret without "
    'passing "--force" (in commands "set", "mv", etc)',
)
@click.option(
    "--render/--no-render",
    default=settings.DEFAULTS.render,
    help="Render templated values",
)
@click.option(
    "-v",
    "--verbose",
    is_eager=True,
    callback=set_verbosity,
    count=True,
    help="Use multiple times to increase verbosity",
)
@click.option(
    "--config-file",
    is_eager=True,
    callback=load_config,
    help="Config file to use. Use 'no' to disable config file. "
    "Default value: first of " + ", ".join(settings.CONFIG_FILES),
    type=click.Path(),
)
@click.option(
    "-V",
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
)
@handle_errors()
def cli(ctx: click.Context, **kwargs) -> None:
    """
    Interact with a Vault. See subcommands for details.

    All arguments can be passed by environment variables: VAULT_CLI_UPPERCASE_NAME
    (including VAULT_CLI_PASSWORD and VAULT_CLI_TOKEN).

    """
    kwargs.pop("config_file")
    verbose = kwargs.pop("verbose")

    assert ctx.default_map  # make mypy happy
    kwargs.update(extract_special_args(ctx.default_map, os.environ))

    # There might still be files to read, so let's do it now
    kwargs = settings.read_all_files(kwargs)
    saved_settings = kwargs.copy()
    saved_settings.update({"verbose": verbose})

    ctx.obj = client.get_client_class()(**kwargs)  # type: ignore
    ctx.obj.auth()
    ctx.obj.saved_settings = saved_settings


def extract_special_args(
    config: Mapping[str, Any], environ: Mapping[str, str]
) -> Dict[str, Any]:
    result = {}
    for key in ["password", "token"]:
        result[key] = config.get(key)
        env_var_key = "VAULT_CLI_{}".format(key.upper())
        if env_var_key in environ:
            result[key] = environ.get(env_var_key)

    return result


@cli.command("list")
@click.argument("path", required=False, default="")
@click.pass_obj
@handle_errors()
def list_(client_obj: client.VaultClientBase, path: str):
    """
    List all the secrets at the given path. Folders are listed too. If no path
    is given, list the objects at the root.
    """
    result = client_obj.list_secrets(path=path)
    click.echo("\n".join(result))


@cli.command(name="get-all")
@click.option(
    "--flat/--no-flat",
    default=True,
    show_default=True,
    help=("Returns the full path as keys instead of merging paths into a tree"),
)
@click.argument("path", required=False, nargs=-1)
@click.pass_obj
@handle_errors()
def get_all(client_obj: client.VaultClientBase, path: Sequence[str], flat: bool):
    """
    Return multiple secrets. Return a single yaml with all the secrets located
    at the given paths. Folders are recursively explored. Without a path,
    explores all the vault.
    """
    paths = list(path) or [""]

    result = client_obj.get_all_secrets(*paths, flat=flat)

    click.echo(
        yaml.safe_dump(result, default_flow_style=False, explicit_start=True), nl=False
    )


@cli.command()
@click.pass_obj
@click.option(
    "--text/--yaml",
    default=True,
    help=(
        "Returns the value in yaml format instead of plain text."
        "If the secret is not a string, it will always be yaml."
    ),
)
@click.option(
    "-o",
    "--output",
    type=click.File("w"),
    help="File in which to write the secret. "
    "If ommited (or -), write in standard output",
)
@click.argument("name")
@click.argument("key", required=False)
@handle_errors()
def get(
    client_obj: client.VaultClientBase,
    text: bool,
    output: Optional[TextIO],
    key: Optional[str],
    name: str,
):
    """
    Return a single secret value.
    """
    secret = client_obj.get_secret(path=name, key=key)
    force_yaml = isinstance(secret, list) or isinstance(secret, dict)
    if text and not force_yaml:
        if secret is None:
            secret = "null"
        click.echo(secret, file=output)
        return

    click.echo(
        yaml.safe_dump(secret, default_flow_style=False, explicit_start=True),
        nl=False,
        file=output,
    )


def build_kv(attributes: Sequence[str]) -> Generator[Tuple[str, str], None, None]:
    """
    Converts a list of "key=value" to tuples (key, value).
    If the value is "-" then reads the secret from stdin.
    """
    for item in attributes:
        try:
            k, v = item.split("=", 1)
        except ValueError:
            raise click.UsageError(
                f"Expecting 'key=value' arguments. '{ item }' provided."
            )
        if v == "-":
            v = click.get_text_stream("stdin").read()
        yield k, v


@cli.command("set")
@click.pass_obj
@click.option(
    "--update/--clear",
    default=True,
    help="Update the current kv mapping or replace the its content",
)
@click.option(
    "-p",
    "--prompt",
    is_flag=True,
    help="Prompt user for values using a hidden input. Keys name are passed as arguments",
)
@click.option(
    "--file",
    "yaml_file",
    default=None,
    help="Read key/value mapping from a file. A filename of '-' reads the standard input",
    type=click.File(),
)
@click.option(
    "--force/--no-force",
    "-f",
    is_flag=True,
    default=None,
    help="In case the path already holds a secret, allow overwriting it "
    "(this is necessary only if --safe-write is set).",
)
@click.argument("path")
@click.argument("attributes", nargs=-1, metavar="[key=value...]")
@handle_errors()
def set_(
    client_obj: client.VaultClientBase,
    update: bool,
    prompt: bool,
    yaml_file: TextIO,
    path: str,
    attributes: Sequence[str],
    force: Optional[bool],
):
    """
    Set a secret.

    \b
    You can give secrets in 3 different ways:
    - Usage: vault set [OPTIONS] PATH [key=value...]
      directly in the arguments. A value of "-" means that value will be read from the standard input
    - Usage: vault set [OPTIONS] PATH --prompt [key...]
      prompt user for a values using hidden input
    - Usage: vault set [OPTIONS] PATH --file=/path/to/file
      using a json/yaml file
    """
    if bool(attributes) + bool(yaml_file) > 1:
        raise click.UsageError(
            "Conflicting input methods: you can't mix --file and positional argument"
        )

    json_value: types.JSONValue
    if yaml_file:
        json_value = yaml.safe_load(yaml_file)
    elif prompt:
        json_value = {}
        for key in attributes:
            json_value[key] = click.prompt(
                f"Please enter a value for key `{key}` of `{path}`", hide_input=True
            )
    else:
        json_value = dict(build_kv(attributes))

    try:
        client_obj.set_secret(path=path, value=json_value, force=force, update=update)
    except exceptions.VaultOverwriteSecretError as exc:
        raise click.ClickException(
            f"Secret already exists at {exc.path}. Use -f to force overwriting."
        )
    except exceptions.VaultMixSecretAndFolder as exc:
        raise click.ClickException(str(exc))
    click.echo("Done")


@cli.command()
@click.pass_obj
@click.argument("name")
@click.argument("key", required=False)
@handle_errors()
def delete(client_obj: client.VaultClientBase, name: str, key: Optional[str]) -> None:
    """
    Delete a single secret.
    """
    client_obj.delete_secret(path=name, key=key)
    click.echo("Done")


@cli.command("env")
@click.option(
    "-p",
    "--path",
    multiple=True,
    required=True,
    help="Folder or single item. Pass several times to load multiple values. You can use --path mypath=prefix or --path mypath:key=prefix if you want to change the generated names of the environment variables",
)
@click.option(
    "-o",
    "--omit-single-key/--no-omit-single-key",
    is_flag=True,
    default=False,
    help="When the secret has only one key, don't use that key to build the name of the environment variable",
)
@click.argument("command", nargs=-1)
@click.pass_obj
@handle_errors()
def env(
    client_obj: client.VaultClientBase,
    path: Sequence[str],
    omit_single_key: bool,
    command: Sequence[str],
) -> NoReturn:
    """
    Launch a command, loading secrets in environment.

    Strings are exported as-is, other types (including booleans, nulls, dicts, lists)
    are exported JSON-encoded.

    If the path ends with `:key` then only one key of the mapping is used and its name is the name of the key.

    VARIABLE NAMES

    By default the name is build upon the relative path to the parent of the given path (in parameter) and the name of the keys in the value mapping.
    Let's say that we have stored the mapping `{'username': 'me', 'password': 'xxx'}` at path `a/b/c`

    Using `--path a/b` will inject the following environment variables: B_C_USERNAME and B_C_PASSWORD
    Using `--path a/b/c` will inject the following environment variables: C_USERNAME and C_PASSWORD
    Using `--path a/b/c:username` will only inject `USERNAME=me` in the environment.

    You can customize the variable names generation by appending `=SOME_PREFIX` to the path.
    In this case the part corresponding to the base path is replace by your prefix.

    Using `--path a/b=FOO` will inject the following environment variables: FOO_C_USERNAME and FOO_C_PASSWORD
    Using `--path a/b/c=FOO` will inject the following environment variables: FOO_USERNAME and FOO_PASSWORD
    Using `--path a/b/c:username=FOO` will inject `FOO=me` in the environment.
    """
    paths = list(path) or [""]

    env_secrets = {}

    for path in paths:
        path_with_key, _, prefix = path.partition("=")
        path, _, filter_key = path_with_key.partition(":")

        if filter_key:
            secret = client_obj.get_secret(path=path, key=filter_key)
            env_updates = environment.get_envvars_for_secret(
                key=filter_key, secret=secret, prefix=prefix
            )
        else:
            secrets = client_obj.get_secrets(path=path, relative=True)
            env_updates = environment.get_envvars_for_secrets(
                path=path,
                prefix=prefix,
                secrets=secrets,
                omit_single_key=omit_single_key,
            )
        env_secrets.update(env_updates)

    environ = os.environ.copy()
    environ.update(env_secrets)

    environment.exec_command(command=command, environ=environ)


@cli.command("dump-config")
@click.pass_obj
@handle_errors()
def dump_config(client_obj: client.VaultClientBase,) -> None:
    """
    Display settings in the format of a config file.
    """
    assert client_obj.saved_settings
    click.echo(
        yaml.safe_dump(
            client_obj.saved_settings, default_flow_style=False, explicit_start=True
        ),
        nl=False,
    )


@cli.command("delete-all")
@click.option(
    "-f",
    "--force",
    is_flag=True,
    help="If not force, prompt for confirmation before each deletion.",
)
@click.argument("path", required=False, nargs=-1)
@click.pass_obj
@handle_errors()
def delete_all(
    client_obj: client.VaultClientBase, path: Sequence[str], force: bool
) -> None:
    """
    Delete multiple secrets.
    """
    paths = list(path) or [""]

    for secret in client_obj.delete_all_secrets(*paths, generator=True):
        if not force and not click.confirm(text=f"Delete '{secret}'?", default=False):
            raise click.Abort()
        click.echo(f"Deleted '{secret}'")


@cli.command()
@click.argument("source", required=True)
@click.argument("dest", required=True)
@click.option(
    "--force/--no-force",
    "-f",
    is_flag=True,
    default=None,
    help="In case the path already holds a secret, allow overwriting it "
    "(this is necessary only if --safe-write is set).",
)
@click.pass_obj
@handle_errors()
def mv(
    client_obj: client.VaultClientBase, source: str, dest: str, force: Optional[bool]
) -> None:
    """
    Recursively move secrets from source to destination path.
    """
    try:
        for old_path, new_path in client_obj.move_secrets(
            source=source, dest=dest, force=force, generator=True
        ):
            click.echo(f"Move '{old_path}' to '{new_path}'")
    except exceptions.VaultOverwriteSecretError as exc:
        raise click.ClickException(
            f"Secret already exists at {exc.path}. Use -f to force overwriting."
        )
    except exceptions.VaultMixSecretAndFolder as exc:
        raise click.ClickException(str(exc))


@cli.command()
@click.argument(
    "template",
    type=click.Path(exists=True, allow_dash=True, file_okay=True),
    required=True,
)
@click.option(
    "-o",
    "--output",
    type=click.File("w"),
    default="-",
    help="File in which to write the rendered template. "
    "If ommited (or -), write in standard output",
)
@click.pass_obj
@handle_errors()
def template(client_obj: client.VaultClientBase, template: str, output: TextIO) -> None:
    """
    Render the given template and insert secrets in it.

    Rendering is done with Jinja2. A vault() function is exposed that
    receives a path and outputs the secret at this path.

    Search path (see https://jinja.palletsprojects.com/en/2.10.x/api/#jinja2.FileSystemLoader)
    for possible Jinja2 `{% include() %}` statement is set to the template's directory.

    If template is -, standard input will be read and the current working directory becomes the search path.

    """
    if template == "-":
        template_text = sys.stdin.read()
        search_path = pathlib.Path.cwd()
    else:
        with open(template, mode="r") as ftemplate:
            template_text = ftemplate.read()
        search_path = pathlib.Path(template).parent

    result = client_obj.render_template(template_text, search_path=search_path)
    output.write(result)


@cli.command()
@click.pass_obj
@handle_errors()
def lookup_token(client_obj: client.VaultClientBase) -> None:
    """
    Return information regarding the current token
    """
    click.echo(
        yaml.safe_dump(
            client_obj.lookup_token(), default_flow_style=False, explicit_start=True
        ),
        nl=False,
    )


def main():
    # https://click.palletsprojects.com/en/7.x/python3/
    os.environ.setdefault("LC_ALL", "C.UTF-8")
    os.environ.setdefault("LANG", "C.UTF-8")

    return cli()
