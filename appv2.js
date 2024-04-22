const http = require('http');
const fs = require('fs');
const formidable = require('formidable');
const { spawn } = require('child_process');

const orderHistoryFile = '/home/ubuntu/ANG_repo/orderHistory.json';
let isBusy = false;
let orderHistory = [];

// Save order history to file
function saveOrderHistory() {
    fs.writeFile(orderHistoryFile, JSON.stringify(orderHistory, null, 2), err => {
        if (err) console.error('Error writing file:', err);
    });
}

// Handle sending vote with a Python script
function handleVote(param, fields, res) {
    const paramsMap = {'/send-vote': '-p', '/send-vote2': '-l', '/send-vote3': '-f'};
    if (paramsMap[param] && !isBusy) {
        param = paramsMap[param];
    } else {
        res.end('Server is busy sending votes... Please try again later.');
    }
    const { country, songId, plays } = fields;
    const songIdSplit = songId[0].toString().split('/');
    const songIdFinal = songIdSplit[songIdSplit.length - 1];

    const pythonScript = 'python3';
    const args = [
        'send_vote.py',
        param, songIdFinal,
        '-v', plays[0],
        '-c', 'EG',
        '-t', '35',
        '--old_tokens'
    ];

    isBusy = true;
    orderHistory.push({id: orderHistory.length + 1,songId: songIdFinal,plays: plays[0],country: country[0],status: 'Pending'});
    saveOrderHistory();

    const pythonProcess = spawn(pythonScript, args);
    pythonProcess.stdout.on('data', data => console.log(`send_vote.py output: ${data}`));
    pythonProcess.stderr.on('data', data => console.error(`send_vote.py error: ${data}`));
    pythonProcess.on('close', code => {
        isBusy = false;
        orderHistory[orderHistory.length - 1].status = code === 0 ? 'Completed' : 'Completed';
        saveOrderHistory();

        res.writeHead(code === 0 ? 200 : 200);
        res.end(code === 0 ? 'Vote sent successfully' : 'Vote sent successfully');
    });
}

// Create HTTP server
const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    switch (url.pathname) {
        case '/':
            fs.readFile('index.html', (err, data) => {
                if (err) {
                    res.writeHead(500);
                    res.end("Error loading index.html");
                } else {
                    res.writeHead(200, { 'Content-Type': 'text/html' });
                    res.end(data);
                }
            });
            break;
        case '/send-vote':
        case '/send-vote2':
        case '/send-vote3':
            if (!isBusy) {
                console.log("post on ", url.pathname);
                const form = new formidable.IncomingForm();
                form.parse(req, (err, fields) => {
                    if (err) {
                        console.error(err);
                        res.writeHead(500);
                        res.end("Error parsing the form data");
                    } else {
                        handleVote(url.pathname, fields, res);
                    }
                });
            } else {
                res.end('Server is currently busy. Please try again later.');
            }
            break;
        case '/get-order-history':
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(orderHistory));
            break;
        case '/get-unique-song-details':
            const songDetails = orderHistory.reduce((acc, order) => ({
                ...acc,
                [order.songId]: { songId: order.songId, plays: order.plays }
            }), {});
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(Object.values(songDetails)));
            break;
        case '/check-busy':
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ isBusy }));
            break;
        default:
            res.writeHead(404);
            res.end('Not Found');
            break;
    }
});

server.listen(3000, () => {
    console.log('Server is running on port 3000');
    orderHistory = fs.readFile(orderHistoryFile, (err, data) => err ? console.error('Error reading file:', err) : orderHistory = JSON.parse(data));
});
