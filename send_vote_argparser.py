from argparse import ArgumentParser
from sys import argv


def _looks_like_script_args(arguments):
	known_arguments = {
		"-l", "--like",
		"-p", "--play",
		"-f", "--follow",
		"-v", "--votes",
		"-c", "--country",
		"-t", "--threads",
		"--old_tokens"
	}
	return any(argument in known_arguments for argument in arguments)


def build_parser():
	parser = ArgumentParser(description="Voting app for anghami")

	parser.add_argument(
		"-l",
		"--like",
		type=str,
		default=None,
		required=False,
		help="id of music to mass liking"
	)

	parser.add_argument(
		"-p",
		"--play",
		type=str,
		default=None,
		required=False,
		help="id of music to mass playing"
	)

	parser.add_argument(
		"-f",
		"--follow",
		type=str,
		default=None,
		required=False,
		help="id of artist to mass following"
	)

	parser.add_argument(
		"-v",
		"--votes",
		type=int,
		default=None,
		required=False,
		help="number of votes"
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
		if parsed.like is None and parsed.play is None and parsed.follow is None:
			parser.error("one of -l/--like, -p/--play, -f/--follow is required")
		if parsed.votes is None:
			parser.error("-v/--votes is required")
		if parsed.country is None:
			parser.error("-c/--country is required")
		if parsed.threads is None:
			parser.error("-t/--threads is required")

	return parsed


args = parse_args()