from argparse import ArgumentParser
from sys import argv


def _looks_like_script_args(arguments):
	known_arguments = {"-a", "--accounts", "-c", "--country", "-t", "--threads", "--old_tokens"}
	return any(argument in known_arguments for argument in arguments)


def build_parser():
	parser = ArgumentParser(description="Registerer for anghami")

	parser.add_argument(
		"-a",
		"--accounts",
		type=int,
		default=None,
		required=False,
		help="number of accounts"
	)

	parser.add_argument(
		"-c",
		"--country",
		type=str,
		default=None,
		required=False,
		help="country of proxies [EG] or [RU] or [LB]"
	)

	parser.add_argument(
		"-t",
		"--threads",
		type=int,
		default=None,
		required=False,
		help="number of threads to use"
	)

	parser.add_argument(
		"--old_tokens",
		action="store_true",
		required=False,
		help="using old grecaptcha tokens"
	)

	return parser


def parse_args(arguments=None):
	parser = build_parser()
	arguments = list(arguments if arguments is not None else argv[1:])
	parsed, _ = parser.parse_known_args(arguments)

	should_validate = __name__ == "__main__" or _looks_like_script_args(arguments)
	if should_validate:
		if parsed.accounts is None:
			parser.error("-a/--accounts is required")
		if parsed.country is None:
			parser.error("-c/--country is required")
		if parsed.threads is None:
			parser.error("-t/--threads is required")

	return parsed


args = parse_args()