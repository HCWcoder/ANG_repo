const http = require('http');
const fs = require('fs');
const { exec } = require('child_process');
const { fail } = require('assert');
const formidable = require('formidable');
const { spawn } = require('child_process');
const PythonShell = require('python-shell').PythonShell;

let isBusy = false;
let orderHistory = [];

function saveOrderHistory() {
    console.log(orderHistory);
    fs.writeFile('/home/ubuntu/ANG_repo/orderHistory.json', JSON.stringify(orderHistory), err => {
        if (err) console.log('Error writing file:', err);
    });
}

function loadOrderHistory() {
    fs.readFile('/home/ubuntu/ANG_repo/orderHistory.json', (err, data) => {
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
                var splitmsg = songId[0].toString().split('/');
                var songIdsplited = splitmsg[splitmsg.length-1];
                songIdsplited = Number(songId);
                const pythonScript = 'python3';
                const args = [
                    'send_vote.py',
                    '-p', songIdsplited,
                    '-v', plays[0],
                    '-c', 'EG',
                    '-t', '35',
                    '--old_tokens'
                ];


                isBusy = true;
                orderHistory.push({
                    id: orderHistory.length + 1,
                    songId: songIdsplited,
                    plays: plays[0],
                    country: country[0],
                    status: 'Pending' // or 'Completed' based on your application logic
                });
                // saveOrderHistory();
                // Start the Python script using spawn
                const pythonProcess = spawn(pythonScript, args);

                // Handle standard output
                pythonProcess.stdout.on('data', (data) => {
                    console.log(`send_vote.py output: ${data}`);
                });

                // Handle standard error
                pythonProcess.stderr.on('data', (data) => {
                    console.error(`send_vote.py error: ${data}`);
                });

                // Handle script exit
                pythonProcess.on('close', (code) => {
                    console.log("done!");
                    if (code !== 0) {
                        console.error(`send_vote.py exited with code ${code}`);
                        res.writeHead(500);
                        isBusy = false;

                        orderHistory[orderHistory.length - 1].status = 'Error';
                        saveOrderHistory();
                        res.end('Internal Server Error');
                    } else {
                        orderHistory[orderHistory.length - 1].status = 'Completed';
                        saveOrderHistory();
                        isBusy = false;
                        res.writeHead(200);
                        res.end('Vote sent successfully');
                    }
                });
            });
    }else if (req.method === 'GET' && url.pathname === '/send-vote') {
        if (isBusy){
            return res.end('Server is busy sending votes... Please try again later.');
        }
        const songId = url.searchParams.get('songId');
        const plays = url.searchParams.get('plays');
        const country = url.searchParams.get('country');

        const command = `python3 send_vote.py -p ${songId} -v ${plays} -c ${country} -t 50 --old_tokens`;

        isBusy = true;
        orderHistory.push({
            id: orderHistory.length + 1,
            songId: songId,
            plays: plays,
            country: country,
            status: 'Pending' // or 'Completed' based on your application logic
        });
        saveOrderHistory();
        var options = {
            args: [
                `-p ${songId}`,   // Pass song ID as argument
                `-v ${plays}`,    // Pass number of plays as argument
                '-c', 'ALL',  // Pass country as argument
                '-t', '10',       // Assuming '-t' takes '50' as a value
                '--old_tokens'    // Additional argument without a value
            ]
        };
          
        PythonShell.run('send_vote.py', options, function (err, results) {
        if (err){
            console.error(`Error executing send_vote.py: ${err}`);
            res.writeHead(500);
            isBusy = false;

            orderHistory[orderHistory.length - 1].status = 'Error!';
            saveOrderHistory();
            return res.end('Internal Server Error');
        }
        // Results is an array consisting of messages collected during execution
        console.log('results: %j', results);
        orderHistory[orderHistory.length - 1].status = 'Completed';
        isBusy = false;
        console.log(`send_vote.py output: ${stdout}`);
        saveOrderHistory();
        res.writeHead(200);
        res.end('Vote sent successfully');
        });

        // exec(command, (error, stdout, stderr) => {
        //     if (error) {
        //         console.error(`Error executing send_vote.py: ${error}`);
        //         res.writeHead(500);
        //         isBusy = false;

        //         orderHistory[orderHistory.length - 1].status = 'Error!';
        //         saveOrderHistory();
        //         return res.end('Internal Server Error');
        //     }
        //     orderHistory[orderHistory.length - 1].status = 'Completed';
        //     isBusy = false;
        //     console.log(`send_vote.py output: ${stdout}`);
        //     saveOrderHistory();
        //     res.writeHead(200);
        //     res.end('Vote sent successfully');
        // });
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
