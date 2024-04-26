
    const songs = [
        {id: "1122520672", name: "هلالالا"},
        {id: "113354", name: "ورينى"},
        {id: "5101858", name: "اجازة (مع بوسي)"},
        {id: "1084162841", name: "أنا بيروت"},
        {id: "1044059663", name: "دكتور في الحب"},
        {id: "44340737", name: "ما فيش كده"},
        {id: "113154", name: "بناديلك"},
        {id: "110399977", name: "ليالي الصيف"},
        {id: "30641982", name: "برّا"},
        {id: "100375988", name: "ولهان"},
        {id: "93537936", name: "ماشي مع جمال ياسين ورامي شلهوب"},
        {id: "112976", name: "قوللى إزاي"},
        {id: "1056005819", name: "بمبي"},
        {id: "98429417", name: "ماشي ريمكس (مع جمال ياسين & رامي شلهوب)"},
        {id: "115014", name: "كسرتلى السيارة"},
        {id: "1046312345", name: "عيشها بمزاج"},
        {id: "55513868", name: "مين قلك"},
        {id: "1074013011", name: "موج"},
        {id: "55513905", name: "أقولك إيه"},
        {id: "5252117", name: "اجازة (مع بوسي) [ريمكس جيمي حداد]"}
    ];

    function toggleSongList(event) {
        const listContainer = document.getElementById('songList');
        if (listContainer.style.display === 'none' || listContainer.style.display === '') {
            listContainer.style.display = 'block';
            populateSongList();
        } else {
            listContainer.style.display = 'none';
        }
        // Stop event propagation to prevent immediate closing
        event.stopPropagation();
    };

    function populateSongList() {
        const listContainer = document.getElementById('songList');
        listContainer.innerHTML = ''; // Clear existing list
        songs.forEach(song => {
            const div = document.createElement('div');
            div.className = 'song-item';
            div.textContent = `${song.name} (${song.id})`;
            div.onclick = function() {
                document.getElementById('songId').value = song.id;
                toggleSongList(); // Hide the list after selection
            };
            listContainer.appendChild(div);
        });
    };
    document.addEventListener('DOMContentLoaded', function() {
        let wasBusy = false;
        fetch('/get-order-history')
            .then(response => response.json())
            .then(orders => {
                document.getElementById('historyCount').innerText = `[${orders.length}]`;
            orders.sort((a, b) => b.id - a.id);
            // Calculate total plays
            // Initialize sums for plays, likes, and follows
            let totalPlays = 0;
            let totalLikes = 0;
            let totalFollows = 0;

            const tableBody = document.querySelector('table tbody');
            orders.forEach(order => {
                const row = document.createElement('tr');
                let statusColor = '';
                switch (order.status) {
                    case 'Completed':
                        statusColor = 'status-completed';
                        break;
                    case 'Error':
                        statusColor = 'status-completed';
                        break;
                    case 'Pending':
                        statusColor = 'status-pending';
                        break;
                }
                // Update sums based on action type
                switch (order.action) {
                    case 'Play':
                        totalPlays += parseInt(order.plays);
                        break;
                    case 'Like':
                        totalLikes += parseInt(order.plays);
                        break;
                    case 'Follow':
                        totalFollows += parseInt(order.plays);
                        break;
                    default:
                        break;
                }
                let actionContent = '';
                if (order.action === 'Play') {
                    actionContent = order.playsx;
                    actionContent2 = order.playsy;
                } else if (order.action === 'Like') {
                    actionContent = order.likesx;
                    actionContent2 = order.likesy;
                }
                row.innerHTML = `
                    <td>
                        <a href="https://play.anghami.com/song/${order.songId}" target="_blank" title="https://play.anghami.com/song/${order.songId}">
                            ${order.songId}
                        </a>
                    </td>
                    <td>${order.plays}</td>
                    <td>${order.country}</td>
                    <td>${order.action || 'Play'}</td>
                    <td>${actionContent}</td>
                    <td>${actionContent2}</td>
                    <td class="${statusColor}">${order.status}</td>
                `;
                tableBody.appendChild(row);
                });
                // Update the total sums in the HTML
                document.getElementById('tPlays').innerText = `[${totalPlays}]`;
                document.getElementById('tLikes').innerText = `[${totalLikes}]`;
                document.getElementById('tFollow').innerText = `[${totalFollows}]`;
            });
        const checkServerBusy = () => {
            fetch('/check-busy')
                .then(response => response.json())
                .then(data => {
                    const buttons = document.querySelectorAll('button');
                    buttons.forEach(button => {
                        button.disabled = data.isBusy;
                    });
                    const loader = document.querySelector('.loader');
                    const loaderx = document.querySelector('.loader-container');
                    loader.style.display = data.isBusy ? 'block' : 'none'; // Show or hide the loader
                    loaderx.style.display = data.isBusy ? 'block' : 'none'; // Show or hide the loader
                    if (wasBusy && !data.isBusy) {
                        window.location.reload();  // Reload the page once when loader disappears
                    }
                    wasBusy = data.isBusy;
                })
                .catch(error => console.error('Failed to check server status:', error));
        };

        checkServerBusy();
        setInterval(checkServerBusy, 5000);
    });
    document.getElementById('voteForm').addEventListener('submit', function(event) {
        event.preventDefault(); // Prevent the default form submission
        var songId = document.getElementById('songId').value;
        if (!songId) {
            alert('Please enter a Song ID before submitting.');
            return; // Stop the form submission
        }
        console.log('submitting form...');
        const actionUrl = event.submitter.formAction || this.action; // Determine the action based on the clicked button
        //console.log(actionUrl);
        const formData = new FormData(this);
        var playsText = document.getElementById('playsValue').innerText;
        // Extract the number from the text, assuming it follows a format like "+50 [3.00 minutes]"
        var totalMinutes = parseFloat(playsText.match(/\[(.*?) minutes\]/)[1]);
        var durationInSeconds = totalMinutes * 60;
        //console.log('totalMinutes', durationInSeconds);
        if (!isNaN(durationInSeconds)) { // Check if the conversion result is a number
            // startTimer(durationInSeconds); // Start the timer with the calculated duration
            startTimerAndSendToServer(durationInSeconds);
            localStorage.setItem('timerDuration', durationInSeconds); // Store the duration in local storage
            // Refresh the page after the timer ends
            window.location.reload();
        } else {
            console.error("Failed to extract total minutes from text:", playsText);
        }
        fetch(actionUrl, {
            method: 'POST',
            body: formData
        })
        .then(response => response.text())
        .then(data => {
            window.location.reload();
        })
        .catch(error => {
            console.error('Error:', error);
        });
    });
    document.body.addEventListener('click', function(event) {
        const listContainer = document.getElementById('songList');
        const target = event.target;
        if (target !== listContainer && !listContainer.contains(target)) {
            // Click occurred outside the songList container
            listContainer.style.display = 'none';
        }
    });
    function updatePlaysValue(value) {
        const minutesPerThousand = 6; // Assume each 1000 plays takes 6 minutes
        var totalMinutes = (value / 1000) * minutesPerThousand;
        document.getElementById('playsValue').innerText = '+' + value + ' [' + totalMinutes.toFixed(2) + ' minutes]';
    }
    
    function startTimer(duration) {
        //console.log('Timer started with duration:', duration);
        var timer = duration, minutes, seconds;
        //console.log('Timer duration:', timer);
        const display = document.getElementById('timerDisplay');
        display.style.display = 'block'; // Show the timer when starting
        clearInterval(window.countdown); // Clear existing timer if any
        window.countdown = setInterval(function () {
            minutes = parseInt(timer / 60, 10);
            seconds = parseInt(timer % 60, 10);
    
            minutes = minutes < 10 ? "0" + minutes : minutes;
            seconds = seconds < 10 ? "0" + seconds : seconds;
    
            display.textContent = minutes + ":" + seconds; // Update the timer display
    
            if (--timer < 0) {
                clearInterval(window.countdown);
                display.textContent = "00:00"; // Reset timer when it reaches 0
                setTimeout(function() {
                    display.style.display = 'none'; // Hide the timer
                    localStorage.removeItem('timerDuration'); // Remove stored duration after completion
                }, 3000); // 3000 milliseconds = 3 seconds
            } else {
                localStorage.setItem('timerDuration', timer); // Update local storage every second
            }
        }, 1000);
    }
    // Function to start the timer and send the end time to the server
function startTimerAndSendToServer(duration) {
    const endTime = Date.now() + duration * 1000; // Calculate end time
    fetch('/timerpost', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ endTime }) // Send end time to server
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('Failed to send timer end time to server');
        }
        console.log('Timer end time sent to server successfully');
    })
    .catch(error => {
        console.error('Error sending timer end time to server:', error);
    });

    startTimer(duration); // Start the timer locally
}
    function retrieveTimerEndTimeFromServer() {
        fetch('/timerget')
        .then(response => {
            if (!response.ok) {
                throw new Error('Failed to retrieve timer end time from server');
            }
            return response.json();
        })
        .then(data => {
            const { endTime } = data;
            //console.log(endTime);
            
            if (endTime) {
                //console.log('endTime', endTime);
                const currentTime = Date.now();
                //console.log('currentTime',currentTime);
                //console.log('total', (endTime - currentTime));
                if (currentTime < endTime) {
                    const remainingTime = endTime - currentTime;
                    //console.log(remainingTime);
            
                    if (remainingTime > 0) {
                        startTimer(remainingTime/1000); // Start the timer with remaining time in milliseconds
                    }
                } 
            }
        })
        .catch(error => {
            console.error('Error retrieving timer end time from server:', error);
        });
    }
    document.addEventListener('DOMContentLoaded', function() {
        retrieveTimerEndTimeFromServer(); // Retrieve timer end time when the page loads
    });
    document.addEventListener('DOMContentLoaded', function() {
        fetch('/get-unique-song-details')
            .then(response => response.json())
            .then(songDetails => {
                const banner = document.getElementById('songIdBanner');
                songDetails.forEach(detail => {
                    const span = document.createElement('span');
                    span.textContent = `${detail.songId}`;
                    span.style.margin = '5px';
                    span.className = 'spanses';
                    span.style.padding = '5px';
                    span.style.border = '1px solid gray';
                    span.style.cursor = 'pointer';
                    span.onclick = function() { fillInputsWithDetails(detail); };
                    banner.appendChild(span);
                });
            })
            .catch(error => console.error('Error loading song details:', error));
    });

    function fillInputsWithDetails(detail) {
        const songIdInput = document.querySelector('input[name="songId"]');
        const playsInput = document.querySelector('input[name="plays"]');

        songIdInput.value = detail.songId;
        updatePlaysValue(detail.plays);
};

