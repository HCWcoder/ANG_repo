const http = require('http');
const fs = require('fs');
const { exec } = require('child_process');
const { fail } = require('assert');
const formidable = require('formidable');

let isBusy = false;
let orderHistory = [];

function saveOrderHistory() {
    console.log(orderHistory);
    fs.writeFile('E:/backup2/Anghami - Github Repo/ANG_repo/orderHistory.json', JSON.stringify(orderHistory), err => {
        if (err) console.log('Error writing file:', err);
    });
}

function loadOrderHistory() {
    fs.readFile('E:/backup2/Anghami - Github Repo/ANG_repo/orderHistory.json', (err, data) => {
        if (err) {
            console.log('Error reading file:', err);
            return;
        }
        orderHistory = JSON.parse(data);
    });
}

loadOrderHistory();

const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);

    if (req.method === 'GET' && url.pathname === '/') {
        fs.readFile('index.html', (err, data) => {
            if (err) {
                res.writeHead(500);
                return res.end("Error loading index.html");
            }
            res.writeHead(200, { 'Content-Type': 'text/html' });
            res.end(data);
        });
    } else if (req.method === 'POST' && url.pathname === '/send-vote') {
            const form = new formidable.IncomingForm();
    
            form.parse(req, (err, fields, files) => {
                if (err) {
                    console.error(err);
                    res.writeHead(500);
                    res.end("Error parsing the form data");
                    return;
                }
    
                const { country, songId, plays } = fields;
                const command = `python "E:/backup2/Anghami - Github Repo/ANG_repo/send_vote.py" -p ${songId[0]} -v ${plays[0]} -c EG -t 25 --old_tokens`;

                isBusy = true;
                orderHistory.push({
                    id: orderHistory.length + 1,
                    songId: songId[0],
                    plays: plays[0],
                    country: country[0],
                    status: 'Pending' // or 'Completed' based on your application logic
                });
                saveOrderHistory();
                exec(command, (error, stdout, stderr) => {
                    if (error) {
                        console.error(`Error executing send_vote.py: ${error}`);
                        res.writeHead(500);
                        isBusy = false;

                        orderHistory[orderHistory.length - 1].status = 'Error!';
                        saveOrderHistory();
                        return res.end('Internal Server Error');
                    }
                    orderHistory[orderHistory.length - 1].status = 'Completed';
                    saveOrderHistory();
                    isBusy = false;
                    console.log(`send_vote.py output: ${stdout}`);
                    res.writeHead(200);
                    res.end('Vote sent successfully');
                });
            });
    }else if (req.method === 'GET' && url.pathname === '/send-vote') {
        if (isBusy){
            return res.end('Server is busy sending votes... Please try again later.');
        }
        const songId = url.searchParams.get('songId');
        const plays = url.searchParams.get('plays');
        const country = url.searchParams.get('country');

        const command = `python "E:/backup2/Anghami - Github Repo/ANG_repo/send_vote.py" -p ${songId} -v ${plays} -c ${country} -t 50 --old_tokens`;

        isBusy = true;
        orderHistory.push({
            id: orderHistory.length + 1,
            songId: songId,
            plays: plays,
            country: country,
            status: 'Pending' // or 'Completed' based on your application logic
        });
        saveOrderHistory();
        exec(command, (error, stdout, stderr) => {
            if (error) {
                console.error(`Error executing send_vote.py: ${error}`);
                res.writeHead(500);
                isBusy = false;

                orderHistory[orderHistory.length - 1].status = 'Error!';
                saveOrderHistory();
                return res.end('Internal Server Error');
            }
            orderHistory[orderHistory.length - 1].status = 'Completed';
            isBusy = false;
            console.log(`send_vote.py output: ${stdout}`);
            saveOrderHistory();
            res.writeHead(200);
            res.end('Vote sent successfully');
        });
    } else if (req.method === 'GET' && url.pathname === '/get-order-history') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        // console.log(orderHistory);
        res.end(JSON.stringify(orderHistory));
    }else if (req.method === 'GET' && url.pathname === '/check-busy') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ isBusy }));
    }else if (req.method === 'GET' && url.pathname === '/get-unique-song-details') {
        // Create a map to store the most recent details for each unique song ID
        const songDetails = {};
        orderHistory.forEach(order => {
            songDetails[order.songId] = { songId: order.songId, plays: order.plays};
        });
        const uniqueSongDetails = Object.values(songDetails);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(uniqueSongDetails));
    }else {
        res.writeHead(404);
        res.end('Not Found');
    }

});


server.listen(3000, () => {
    console.log('Server is running on port 3000');
});
