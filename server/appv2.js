const http = require('http');
const serveStatic = require('serve-static');
const finalhandler = require('finalhandler');
const path = require('path');
const fs = require('fs');
const formidable = require('formidable');
const { spawn } = require('child_process');
const axios = require('axios');
const cheerio = require('cheerio');
const bodyParser = require('body-parser');
// Directory paths
const publicDir = path.join(__dirname, '../public');
const dataDir = path.join(__dirname, '../data');
const orderHistoryFile = path.join(dataDir, 'orderHistory.json');
const timerFile = path.join(dataDir, 'timerEndTime.json');
let isBusy = false;
let orderHistory = [];

// Save order history to file
function saveOrderHistory() {
    fs.writeFile(orderHistoryFile, JSON.stringify(orderHistory, null, 2), err => {
        if (err) console.error('Error writing file:', err);
    });
}

async function getPlayLikes(songId, maxRetries = 10, retryDelay = 2000) {
    let retries = 0;
    while (retries < maxRetries) {
        try {
            const headers = {
                'Upgrade-Insecure-Requests': '1',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            };

            const url = `https://play.anghami.com/song/${songId}`;
            const response = await axios.get(url, { headers: headers });
            const $ = cheerio.load(response.data);

            const likesSelector = 'div[class="section-details flex"] > span:nth-of-type(2) > div[class="font-weight-bold value"]';
            const playsSelector = 'div[class="section-details flex"] > span:nth-of-type(3) > div[class="font-weight-bold value"]';

            const likesText = $(likesSelector).text().trim();
            const playsText = $(playsSelector).text().trim();

            // Parse likes and plays
            const likes = parseLikesPlays(likesText);
            const plays = parseLikesPlays(playsText);
            console.log('Likes:', likes, 'Plays:', plays);
            if (likes === 0 || plays === 0) {
                retries++;
                await new Promise(resolve => setTimeout(resolve, retryDelay)); // Wait before retrying
                continue; // Retry without returning
            }

            return { likes, plays };
        } catch (error) {
            console.error('Error fetching data:', error);
            retries++;
            await new Promise(resolve => setTimeout(resolve, retryDelay)); // Wait before retrying
        }
    }
    return { likes: 'Error', plays: 'Error' }; // Return error if max retries reached
}

function parseLikesPlays(text) {
    const multiplier = { 'K': 1000, 'M': 1000000 }; // Multipliers for 'K' (thousands) and 'M' (millions)
    const match = text.match(/^([\d.]+)([KM])?\s/); // Extract numeric part and multiplier
    if (match) {
        const value = parseFloat(match[1]);
        const unit = match[2];
        return unit ? Math.round(value * multiplier[unit]) : value;
    }
    return 0; // Return 0 if no match found
}

function startTimer(duration) {
    let timer = duration;
    const intervalId = setInterval(() => {
        if (--timer < 0) {
            clearInterval(intervalId);
            timer = duration; // Reset timer when it reaches 0
        }
        io.emit('timerUpdate', timer); // Send timer update to all connected clients
    }, 1000);
}

// Handle sending vote with a Python script
function handleVote(param, fields, res) {
    const paramsMap = {'/send-vote': '-p', '/send-vote2': '-l', '/send-vote3': '-f'};
    const actionMap = {'-p': 'Play', '-l': 'Like', '-f': 'Follow'};
    if (paramsMap[param] && !isBusy) {
        param = paramsMap[param];
    } else {
        res.end('Server is busy sending votes... Please try again later.');
    }
    const { country, songId, plays } = fields;
    const songIdSplit = songId[0].toString().split('/');
    const songIdFinal = songIdSplit[songIdSplit.length - 1];

    const actionType = actionMap[param] || 'Unknown Action';
    const pythonScript = 'python3';
    const args = [
        'send_vote.py',
        param, songIdFinal,
        '-v', plays[0],
        '-c', 'EG',
        '-t', '35',
        '--old_tokens'
    ];
    var likesx = 0;
    var playsx = 0;
    getPlayLikes(songIdFinal)  // Replace 1122520672 with any valid song ID
    .then(data => {
        orderHistory.push({
            id: orderHistory.length + 1,
            songId: songIdFinal,
            plays: plays[0],
            country: country[0],
            action: actionType,
            status: 'Pending',
            likesx: data.likes,
            playsx: data.plays,
            likesy: 0,
            playsy: 0
        });
        saveOrderHistory();
    })
    .catch(error => console.error('Failed to get likes and plays:', error));

    isBusy = true;
    
    const pythonProcess = spawn(pythonScript, args);
    pythonProcess.stdout.on('data', data => console.log(`send_vote.py output: ${data}`));
    pythonProcess.stderr.on('data', data => console.error(`send_vote.py error: ${data}`));
    pythonProcess.on('close', code => {
        isBusy = false;
        orderHistory[orderHistory.length - 1].status = code === 0 ? 'Completed' : 'Completed';
        saveOrderHistory();
        getPlayLikes(songIdFinal)  // Replace 1122520672 with any valid song ID
        .then(data => {
            orderHistory[orderHistory.length - 1].likesy = data.likes;
            orderHistory[orderHistory.length - 1].playsy = data.plays;
            saveOrderHistory();
        })
        .catch(error => console.error('Failed to get likes and plays:', error));

        res.writeHead(code === 0 ? 200 : 200);
        res.end(code === 0 ? 'Vote sent successfully' : 'Vote sent successfully');
    });
    
}

const serve = serveStatic(publicDir, {
    'index': ['index.html', 'index.htm']
});

// Create HTTP server
const server = http.createServer((req, res) => {
    const done = finalhandler(req, res);
    serve(req, res, () => {
        const url = new URL(req.url, `http://${req.headers.host}`);
        console.log(url.pathname);
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
            case '/timerget':
                fs.readFile(timerFile, (err, data) => {
                    if (err) {
                        console.error('Error reading timer file:', err);
                        res.writeHead(500, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ error: 'Internal Server Error' }));
                    } else {
                        const { endTime } = JSON.parse(data);
                        const responseData = JSON.stringify({ endTime });
                        res.writeHead(200, { 'Content-Type': 'application/json' });
                        res.end(responseData);
                    }
                });
                break;
                case '/timerpost':
                    let body = '';
                    req.on('data', (chunk) => {
                        body += chunk.toString();
                    });
                    req.on('end', () => {
                        try {
                            const { endTime } = JSON.parse(body);
                            //console.log(endTime);
                            fs.writeFile(timerFile, JSON.stringify({ endTime }), (err) => {
                                if (err) {
                                    console.error('Error writing timer file:', err);
                                    res.writeHead(500, { 'Content-Type': 'application/json' });
                                    res.end(JSON.stringify({ error: 'Internal Server Error' }));
                                } else {
                                    res.writeHead(200, { 'Content-Type': 'application/json' });
                                    res.end(JSON.stringify({ message: 'Timer end time saved successfully' }));
                                }
                            });
                        } catch (error) {
                            console.error('Error parsing request body:', error);
                            res.writeHead(400, { 'Content-Type': 'application/json' });
                            res.end(JSON.stringify({ error: 'Invalid JSON in request body' }));
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
                done();  // Let finalhandler manage not found errors
                break;
        }
    });
});

server.listen(3000, () => {
    console.log('Server is running on port 3000');
    orderHistory = fs.readFile(orderHistoryFile, (err, data) => err ? console.error('Error reading file:', err) : orderHistory = JSON.parse(data));
});
