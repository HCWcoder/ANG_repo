from argparse import ArgumentParser


parser = ArgumentParser(description="Registerer for anghami")

parser.add_argument(
	"-a",
	"--accounts",
	type=int,
	required=True,
	help="number of accounts"
)

parser.add_argument(
	"-c",
	"--country",
	type=str,
	required=True,
	help="country of proxies [EG] or [RU] or [LB]"
)

parser.add_argument(
	"-t",
	"--threads",
	type=int,
	required=True,
	help="number of threads to use"
)

parser.add_argument(
	"--old_tokens",
	action="store_true",
	required=False,
	help="using old grecaptcha tokens"
)

args = parser.parse_args()